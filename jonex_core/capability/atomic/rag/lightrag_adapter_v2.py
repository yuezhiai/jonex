#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""LightRAG v2 适配器（形态a）

把 atomic-rag v2（raganything.service 的 TaskManager HTTP 模式）封装成平台标准
原子能力，由 `deploy/start_capability.py` 加载运行——因此白拿平台外壳：
`verify_internal_service` 内部认证 + 服务发现 + 心跳 + 统一异常。

设计原则（见 docs/atomic-rag-v1-to-v2-migration-plan.md §5 P1）：
- **薄壳**：本类只做「能力外壳 + action 分派 + 返回包装」，
  action 的真实逻辑**复用 vendored `atomic-rag-server-v2.py` 的同一组 ActionRegistry
  handler**（通过 importlib 加载，零 vendored 改动、零契约漂移）。
- **单一 import 收敛点**：v2 技术栈的装配集中在 `_assemble_stack()`，
  三层重构落地后只需改这一处 import 路径。
- **生产契约以本类为准**；vendored `atomic-rag-server-v2.py` 仅本地调试入口。

能力 ID：`atomic.rag.lightrag.v1`（与原 v1 `LightRAGAdapter`（已退役删除）完全一致，capability_id 不变）。
"""
import importlib.util
import os
import time
import uuid
from typing import Optional

from jonex_core.capability.atomic.rag.base import BaseRAGCapability
from jonex_core.capability.models import (
    CapabilityMetadata,
    CapabilityRequest,
    CapabilityResponse,
    CapabilityType,
)
from jonex_core.common.exceptions import CapabilityInvokeError
from jonex_core.common.logger import get_logger
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

logger = get_logger("capability.atomic.rag.lightrag_v2")

# ── [jonex] MPS 视频分析辅助（方案 C：MPS 和本地 ffmpeg 两条独立路径）──
_MPS_ENABLED = os.getenv("MPS_ENABLED", "").strip().lower() in ("true", "1", "yes")
_VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    '.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv', '.m4v', '.mpg', '.mpeg', '.3gp',
})


def _is_video_file(file_path: str) -> bool:
    """按扩展名判断是否为视频文件。"""
    return os.path.splitext(file_path or "")[1].lower() in _VIDEO_EXTENSIONS


def _build_mps_cos_url(storage_key: str) -> str:
    """从 COS storage_key 构造完整 COS URL（供 MPS 后端使用）。

    MPS 需要 ``https://{bucket}.cos.{region}.myqcloud.com/{key}`` 格式的 COS 地址，
    本地缓存路径无法被 MPS 识别。
    """
    bucket = os.getenv("COS_BUCKET", "")
    region = os.getenv("COS_REGION", "")
    if not bucket or not region or not storage_key:
        return ""
    return f"https://{bucket}.cos.{region}.myqcloud.com/{storage_key}"


class LightRAGAdapterV2(BaseRAGCapability):
    """v2 TaskManager(HTTP 模式) 的平台能力薄壳。"""

    def __init__(self) -> None:
        super().__init__()
        self._tm = None            # raganything.service.task_manager.TaskManager
        self._cr = None            # raganything.service.config_resolver.ConfigResolver
        self._pcm = None           # raganything.service.prompt_config_manager.PromptConfigManager
        self._actions = None       # atomic-rag-server-v2.py 的 ActionRegistry
        self._started = False

    # ── 元数据 ──────────────────────────────────────────────
    def _build_metadata(self) -> CapabilityMetadata:
        return CapabilityMetadata(
            capability_id="rag.lightrag",
            capability_name="LightRAG RAG 能力 (v2/HTTP)",
            capability_type=CapabilityType.ATOMIC,
            version="v1",
            description="RAG-Anything v2 TaskManager（HTTP 模式，读写经 lightrag-server :9621）",
        )

    # ── 生命周期 ────────────────────────────────────────────
    async def initialize(self) -> None:
        if self._started:
            return
        self._assemble_stack()
        # HTTP mode: init multimodal processors (VLM/ASR) without embedded LightRAG
        if self._pipeline_executor is not None:
            await self._pipeline_executor._init_processors_via_builder()
        await self._tm.start()
        self._started = True
        logger.info("LightRAGAdapterV2 初始化完成（v2 HTTP 模式）")

    async def shutdown(self) -> None:
        if self._tm is not None and self._started:
            try:
                await self._tm.shutdown()
            finally:
                self._started = False

    def _assemble_stack(self) -> None:
        """装配 v2 技术栈 —— **唯一 import 收敛点**。

        复刻 `atomic-rag-server-v2.py main()` 的 wiring：
        ModelFactory + HttpLightRagClient + RAGAnything(http_client) + TaskManager + ConfigResolver。
        三层重构落地后只改本方法内的 import 路径。
        """
        import raganything
        from raganything.service.model_factory import ModelFactory
        from raganything.service.task_manager import TaskManager
        from raganything.service.config_resolver import ConfigResolver
        from raganything.service.http_lightrag_client import HttpLightRagClient
        from raganything.service.prompt_config_manager import PromptConfigManager
        from raganything.raganything import RAGAnything
        from raganything.config import RAGAnythingConfig

        # [jonex] §table-grid-v2 L4.2: 摘要/描述 prompt 语言（受 RAG_PROMPT_LANG
        # 控制，默认 zh——线上 104/1063 个英文表格摘要正是未切中文的产物）。
        # set_prompt_language 是进程级全局，必须在 RAGAnything 消费 PROMPTS 之前调用；
        # 影响所有模态（图片/公式/音视频），dev 全模态回归后再上生产（§6 风险表）。
        from raganything.prompt_manager import set_prompt_language

        prompt_lang = os.getenv("RAG_PROMPT_LANG", "zh")
        try:
            set_prompt_language(prompt_lang)
            logger.info(
                "[jonex] §table-grid-v2 L4.2: prompt 语言切换为 %s", prompt_lang,
            )
        except Exception:
            logger.warning(
                "[jonex] §table-grid-v2 L4.2: prompt 语言 %s 不可用，保持默认（en）",
                prompt_lang,
            )

        # config 目录：默认相对 raganything 包所在目录（容器内 /opt/raganything），可 env 覆盖
        rag_base = os.path.dirname(os.path.dirname(os.path.abspath(raganything.__file__)))
        profiles_dir = os.getenv("RAG_PROFILES_DIR") or os.path.join(rag_base, "config", "profiles")
        presets_dir = os.getenv("RAG_PRESETS_DIR") or os.path.join(rag_base, "config", "presets")
        base_dir = os.getenv("RAG_SERVICE_BASE_DIR") or os.getenv("WORKING_DIR") or "./rag_service_data"

        mf = ModelFactory(profiles_dir=profiles_dir)
        http_client = HttpLightRagClient()
        vlm_result = mf.build_vlm()
        vlm_func = vlm_result["func"] if vlm_result else None
        vlm_bound = vlm_result["bound"] if vlm_result else None
        # [jonex] 主解析提示词：prompt 配置管理器（CRUD + pipeline 消费共用同一实例/同一目录）
        pcm = PromptConfigManager(
            base_dir=os.getenv("PROMPT_CONFIG_DIR") or os.path.join(base_dir, "prompt_configs")
        )

        # Build LLM function for video/audio MapReduce summarization
        llm_func = mf.build_llm() if hasattr(mf, 'build_llm') else None
        if llm_func is None:
            # Fallback: use _metered_llm with env vars (LLM_BINDING_HOST / LLM_MODEL)
            llm_host = os.getenv("LLM_BINDING_HOST", "")
            llm_model = os.getenv("LLM_MODEL", "deepseek-v4-flash-202605")
            if llm_host:
                llm_func = mf._metered_llm(llm_model)
                logger.info(f"LLM (HTTP mode): {llm_model} @ {llm_host}")
            else:
                logger.warning("LLM not configured — video/audio summarization will be skipped")

        # RAGAnything 为 dataclass：doc_parser 由 __post_init__ 从 config.parser 构造，
        # 不接受 doc_parser 入参。用 config.parser=RAG_PARSER 设默认解析器（per-task preset
        # 会在 process_document_complete 内按 ctx.parser_type 覆盖，见 preset 链路改造）。
        pipeline_executor = RAGAnything(
            config=RAGAnythingConfig(
                working_dir=base_dir,
                parser=os.getenv("RAG_PARSER", "mineru"),
            ),
            vlm_model_func=vlm_func,
            vlm_bound=vlm_bound,
            llm_model_func=llm_func,   # video/audio MapReduce 总结
            embedding_func=None,       # :9621 内部处理 embedding
            http_client=http_client,
        )
        tm = TaskManager(
            base_dir=base_dir,
            model_factory=mf,
            http_client=http_client,
            pipeline_executor=pipeline_executor,
            prompt_config_manager=pcm,
        )
        cr = ConfigResolver(profiles_dir=profiles_dir, presets_dir=presets_dir)
        tm.set_config_resolver(cr)

        self._pipeline_executor = pipeline_executor
        self._tm = tm
        self._cr = cr
        self._pcm = pcm
        self._actions = self._load_action_registry(rag_base)

    @staticmethod
    def _load_action_registry(rag_base: str):
        """importlib 加载 vendored `atomic-rag-server-v2.py` 的 ActionRegistry。

        零 vendored 改动、零契约漂移（与调试 server 共用同一组 handler）。
        文件名含 '-' 不能正常 import，故按路径加载；只取其模块级 ActionRegistry
        （@register 装饰器在 import 时已填充），不调用 main()。
        """
        server_path = os.path.join(rag_base, "atomic-rag-server-v2.py")
        spec = importlib.util.spec_from_file_location("_atomic_rag_server_v2", server_path)
        if spec is None or spec.loader is None:
            raise CapabilityInvokeError(message=translate("err.rag.server_locate_failed", fallback="无法定位 atomic-rag-server-v2.py"), details={"server_path": server_path})
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.ActionRegistry

    # ── 调用入口（平台通过 registry.invoke → execute） ──────
    async def validate_input(self, request: CapabilityRequest) -> bool:
        return bool(request.payload.get("action"))

    async def execute(self, request: CapabilityRequest) -> CapabilityResponse:
        action = request.payload.get("action")
        res = await self._dispatch(action, request.payload, request.tenant_id)
        return CapabilityResponse(
            request_id=request.request_id,
            success=bool(res.get("success")),
            code=int(res.get("code", 0)),
            message=res.get("message", ""),
            data=res.get("data"),
        )

    async def _dispatch(self, action: Optional[str], payload: dict, tenant_id: str) -> dict:
        """调用 vendored ActionRegistry 的同名 handler，统一返回 {success,code,message,data}。"""
        from fastapi import HTTPException

        # 显式租户校验（唯一入口）：拒绝 空/default/default_tenant/system，
        # 等价于 vendored v2 server /invoke 的 _FORBIDDEN_TENANTS 拦截。
        # 放在 try 之前，让 TenantIsolationError 正常抛出、由全局异常处理器映射。
        tenant_id = require_tenant(tenant_id)

        if not action or self._actions is None:
            return {"success": False, "code": 400, "message": translate("err.capability.unsupported_action", params={"action": action}, fallback=f"不支持的 action: {action}"), "data": None}
        handler = self._actions.get(action)
        if handler is None:
            return {"success": False, "code": 400, "message": translate("err.capability.unsupported_action", params={"action": action}, fallback=f"不支持的 action: {action}"), "data": None}
        p = dict(payload)
        p["action"] = action
        try:
            # [jonex] R7-2b：COS 下载异步化 —— 默认移入任务 pipeline（_execute_pipeline_http 的
            # FetchObject 段），invoke 毫秒级返回。设 RAG_COS_FETCH_IN_TASK=false 回退同步路径。
            # 仅 insert/retry（整文件重解析）需要文件；retry_ontology_extract 只读 :9621 存储，无需下载。
            if action in ("insert", "retry"):
                if os.getenv("RAG_COS_FETCH_IN_TASK", "true").lower() in ("1", "true", "yes", "on"):
                    # 新路径：透传 storage_backend=cos + storage_key，下载在任务 pipeline 内完成
                    pass
                else:
                    # 旧路径（回退）：同步下载 COS 对象到本地
                    p = await self._localize_input(p, tenant_id)
            return await handler(
                p, tenant_id,
                task_manager=self._tm, config_resolver=self._cr,
                prompt_config_manager=self._pcm,
            )
        except HTTPException as e:  # handler 用 HTTPException 表达 4xx/5xx
            return {"success": False, "code": e.status_code, "message": translate("err.capability.upstream_error", params={"status": str(e.status_code)}, fallback=f"能力服务返回错误: HTTP {e.status_code}"), "data": None}
        except Exception as e:  # noqa: BLE001 — 统一兜底，避免裸异常穿透 registry
            logger.exception("LightRAGAdapterV2 action=%s 执行失败: %s", action, e)
            return {"success": False, "code": 500, "message": translate("err.capability.invoke_failed", fallback="能力调用失败"), "data": None}

    # ── W3：COS 输入本地化（放能力侧，让 atomic-rag 统一解析本地文件） ──
    def _cos_cache_dir(self) -> str:
        """COS 下载缓存目录：优先 RAG_COS_CACHE_DIR；否则落共享输入卷/工作目录下的 _cos_cache。

        建议为持久卷（异步 worker 稍后解析；容器重启后任务恢复仍需该文件）。
        """
        base = (
            os.getenv("RAG_COS_CACHE_DIR")
            or os.path.join(
                os.getenv("KB_INPUT_DIR") or os.getenv("WORKING_DIR") or "./rag_service_data",
                "_cos_cache",
            )
        )
        os.makedirs(base, exist_ok=True)
        return base

    def _sweep_cos_cache(self) -> None:
        """机会式清理：删除超过 TTL 的 COS 缓存文件（best-effort，无需 per-task hook）。

        因能力侧下载 + 异步 worker 解析，缺少"任务终态 finally 清理"点，改用 TTL 兜底。
        TTL 需 > 单文档最大解析耗时 + 重启窗口，默认 24h。
        """
        ttl = int(os.getenv("RAG_COS_CACHE_TTL_SEC", "86400"))
        now = time.time()
        try:
            d = self._cos_cache_dir()
            for name in os.listdir(d):
                fp = os.path.join(d, name)
                try:
                    if os.path.isfile(fp) and now - os.path.getmtime(fp) > ttl:
                        os.remove(fp)
                except OSError:
                    pass
        except OSError:
            pass

    async def _localize_input(self, payload: dict, tenant_id: str) -> dict:
        """对象存储后端（cos/s3）时把对象下载到本地，改写 file_path 为本地路径、backend 置 local。

        [jonex] S3 兼容：按 payload 自身的 storage_backend 选客户端（get_object_storage_for），
        混合数据（部分文档 cos、部分 s3）时全局单例会用错后端。
        """
        backend = (payload.get("storage_backend") or "local").strip().lower()
        if backend not in ("cos", "s3"):
            return payload
        storage_key = payload.get("storage_key")
        if not storage_key:
            return payload  # 无 key，交给下游按原样报错（不隐藏问题）

        from jonex_core.common.object_storage import get_object_storage_for

        self._sweep_cos_cache()  # 机会式清理过期缓存
        base_name = os.path.basename(payload.get("file_path") or storage_key) or "cos_object"
        local_path = os.path.join(self._cos_cache_dir(), f"{uuid.uuid4().hex}_{base_name}")
        await get_object_storage_for(backend).get_to_path(storage_key, local_path)
        logger.info("W3 %s 本地化: %s → %s", backend, storage_key, local_path)

        p = dict(payload)
        p["file_path"] = local_path
        p["storage_backend"] = "local"  # 下游 atomic-rag 统一按本地文件解析

        # [jonex] 方案 C：MPS 视频分析需要 COS URL（本地缓存路径无法被 MPS 识别）。
        # 视频文件在 COS 场景下保留原始 COS 地址，供下游 MPS backend 使用。
        if _MPS_ENABLED and _is_video_file(local_path):
            mps_url = _build_mps_cos_url(storage_key)
            if mps_url:
                p["mps_video_url"] = mps_url
                logger.info("W3 MPS COS URL 已保留: %s", mps_url)

        return p

    # ── BaseRAGCapability 抽象方法（LOCAL 直连时用；REMOTE 走 execute） ──
    async def insert(
        self,
        file_path: str,
        tenant_id: str,
        knowledge_base_id: str,
        output_dir: Optional[str] = None,
        ontology_schema: Optional[dict] = None,
        preset: Optional[str] = None,
        **kwargs,
    ) -> dict:
        payload = {
            "file_path": file_path,
            "knowledge_base_id": knowledge_base_id,
            "output_dir": output_dir,
            "ontology_schema": ontology_schema,
        }
        if preset:
            payload["preset"] = preset
        # 兼容 LOCAL 直连传入的 document_id / storage_backend / storage_key /
        # execution_mode / prompt_ids。
        # [jonex] 键集与 LocalRAGClient.insert 的 _extra（client.py）逐一对应，
        # 新加透传参数时两处需同步。
        for k in ("document_id", "storage_backend", "storage_key", "execution_mode", "prompt_ids"):
            if kwargs.get(k) is not None:
                payload[k] = kwargs[k]
        return self._unwrap(await self._dispatch("insert", payload, tenant_id), "insert")

    async def query(
        self,
        query: str,
        tenant_id: str,
        mode: str = "hybrid",
        top_k: int = 5,
        *,
        knowledge_base_id: str,
        trace_id: str = "",
        user_id: str = "",
        only_need_context: bool = False,
    ) -> str:
        # [jonex] 方案 A + 计量维度：与 REMOTE 链（atomic-rag-server-v2 handle_query）同构，
        # 对齐 v1 _jonex_query_headers / only_need_context 透传。
        payload = {
            "query": query, "mode": mode, "top_k": top_k,
            "knowledge_base_id": knowledge_base_id,
            "trace_id": trace_id, "user_id": user_id,
            "only_need_context": only_need_context,
        }
        data = self._unwrap(await self._dispatch("query", payload, tenant_id), "query")
        return (data or {}).get("answer", "")

    async def query_detailed(
        self,
        query: str,
        tenant_id: str,
        mode: str = "hybrid",
        top_k: int = 5,
        *,
        knowledge_base_id: str,
        trace_id: str = "",
        user_id: str = "",
        only_need_context: bool = False,
    ) -> dict:
        # [jonex] v1 退役：补 LOCAL 直连面的 query_detailed（v1 独有的方法）。
        # payload 与 query 同构（vendored handle_query 的 data 本就是 {answer, references}），
        # 返回结构须与 RemoteRAGClient.query_detailed 对齐，不得只取 answer 字符串。
        payload = {
            "query": query, "mode": mode, "top_k": top_k,
            "knowledge_base_id": knowledge_base_id,
            "trace_id": trace_id, "user_id": user_id,
            "only_need_context": only_need_context,
        }
        data = self._unwrap(await self._dispatch("query", payload, tenant_id), "query")
        return {
            "answer": (data or {}).get("answer", ""),
            "references": (data or {}).get("references", []),
        }

    async def delete(
        self,
        doc_id: str,
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
    ) -> bool:
        payload = {"doc_id": doc_id, "knowledge_base_id": knowledge_base_id}
        data = self._unwrap(await self._dispatch("delete", payload, tenant_id), "delete")
        return bool((data or {}).get("success"))

    async def delete_orphan_chunk(
        self,
        chunk_id: str,
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
    ) -> bool:
        """[jonex] O7：孤儿 chunk 强制清理（action delete_orphan_chunk）。"""
        payload = {"chunk_id": chunk_id, "knowledge_base_id": knowledge_base_id}
        data = self._unwrap(
            await self._dispatch("delete_orphan_chunk", payload, tenant_id),
            "delete_orphan_chunk",
        )
        return bool((data or {}).get("deleted"))

    async def delete_batch(
        self,
        doc_ids: list[str],
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
        document_id: str = "",
        trace_id: str = "",
    ) -> dict:
        """[jonex] 批量删除——单次 dispatch 提交全部 doc_ids，
        LightRAG 仅执行一次 rebuild_knowledge_from_chunks。

        [jonex] R4：透传 document_id / trace_id，
        使删除/rebuild 的 LLM 调用能正确归因。
        """
        payload: dict = {
            "doc_ids": list(doc_ids),
            "knowledge_base_id": knowledge_base_id,
        }
        if document_id:
            payload["document_id"] = document_id
        if trace_id:
            payload["trace_id"] = trace_id
        data = self._unwrap(
            await self._dispatch("delete_batch", payload, tenant_id), "delete_batch",
        )
        return data or {"success": False, "accepted": [], "failed": doc_ids}

    async def get_task_status(self, task_id: str, tenant_id: str) -> dict:
        # 注意：not_found 时 handler 返回 success=False；此处直接回传 data（含 status=not_found），
        # 不抛异常，便于 LOCAL 直连方也能拿到 not_found 语义。
        res = await self._dispatch("get_task_status", {"task_id": task_id}, tenant_id)
        return res.get("data") or {}

    async def get_doc_chunks(
        self,
        doc_id: str,
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
    ) -> dict:
        """按 doc_id 查文档 chunks（含行号/时间轴位置元数据），不依赖 task。

        对应 vendored action `get_doc_chunks`。not_found 时 handler 返回
        success=False（code=40405），此处与 get_task_status 一致，直接回传 data
        （含 doc_id / total=0 / chunks=[]），不抛异常，便于调用方拿到空结果语义。

        返回 data 形如 {"doc_id", "total", "chunks": [{chunk_id, content, tokens,
        chunk_order_index, page_idx, text_idx, line_start, line_end, ...}]}。
        """
        res = await self._dispatch(
            "get_doc_chunks",
            {"doc_id": doc_id, "knowledge_base_id": knowledge_base_id},
            tenant_id,
        )
        return res.get("data") or {}

    async def export_doc(
        self,
        doc_id: str,
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
        fmt: str = "json",
    ) -> Optional[dict]:
        """按 doc_id 导出全文 + 实体 + 关系（不依赖 task）。对应 action export_doc。

        not_found 时返回 None（handler success=False/40405），不抛异常。
        """
        res = await self._dispatch(
            "export_doc",
            {"doc_id": doc_id, "knowledge_base_id": knowledge_base_id, "format": fmt},
            tenant_id,
        )
        return res.get("data")

    async def retry(
        self,
        file_path: str,
        tenant_id: str,
        knowledge_base_id: str,
        *,
        preset: Optional[str] = None,
        output_dir: Optional[str] = None,
        ontology_schema: Optional[dict] = None,
        **kwargs,
    ) -> Optional[dict]:
        """以 force_reparse=true 重新解析同一文件。对应 action retry。

        与 insert 一致：cos 输入会在 _dispatch 内本地化（_localize_input）。
        兼容 document_id / storage_backend / storage_key（经 kwargs 传入）。
        """
        payload = {
            "file_path": file_path,
            "knowledge_base_id": knowledge_base_id,
            "output_dir": output_dir,
            "ontology_schema": ontology_schema,
        }
        if preset:
            payload["preset"] = preset
        for k in ("document_id", "storage_backend", "storage_key"):
            if kwargs.get(k) is not None:
                payload[k] = kwargs[k]
        return self._unwrap(await self._dispatch("retry", payload, tenant_id), "retry")

    async def update_chunk(
        self,
        chunk_id: str,
        new_content: str,
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
        file_source: str = "",
        expected_content_hash: Optional[str] = None,
    ) -> Optional[dict]:
        """修改 chunk 内容并写回（经 :9621 服务端事务）。对应 action update_chunk。

        失败（如 409 乐观锁冲突 / 404 不存在）抛 CapabilityInvokeError，code 在 details。
        """
        payload = {
            "chunk_id": chunk_id,
            "new_content": new_content,
            "knowledge_base_id": knowledge_base_id,
            "file_source": file_source,
        }
        if expected_content_hash is not None:
            payload["expected_content_hash"] = expected_content_hash
        return self._unwrap(
            await self._dispatch("update_chunk", payload, tenant_id), "update_chunk"
        )

    async def retry_ontology_extract(
        self,
        document_id: str,
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
        file_path: str = "",
        ontology_schema: Optional[dict] = None,
    ) -> Optional[dict]:
        """重新触发本体抽取（含幂等保护）。对应 action retry_ontology_extract。"""
        payload = {
            "document_id": document_id,
            "knowledge_base_id": knowledge_base_id,
            "file_path": file_path,
            "ontology_schema": ontology_schema,
        }
        return self._unwrap(
            await self._dispatch("retry_ontology_extract", payload, tenant_id),
            "retry_ontology_extract",
        )

    async def upsert_preset(
        self,
        preset_name: str,
        config: dict,
        tenant_id: str,
        *,
        description: str = "",
        version: str = "1.0",
        kb_id: str = "",
        updated_by: str = "sidecar",
    ) -> Optional[dict]:
        """创建/更新 preset（支持 global/tenant/kb 作用域）。对应 action upsert_preset。"""
        payload = {
            "preset_name": preset_name,
            "config": config,
            "description": description,
            "version": version,
            "kb_id": kb_id,
            "updated_by": updated_by,
        }
        return self._unwrap(
            await self._dispatch("upsert_preset", payload, tenant_id), "upsert_preset"
        )

    # ── Storage 读取（经 :9621 实时数据；读操作回传 data、不抛异常） ──

    async def get_storage_summary(
        self, tenant_id: str, *, knowledge_base_id: str,
    ) -> Optional[dict]:
        """图谱概览（实体/关系/chunk 计数）。对应 action get_storage_summary。"""
        res = await self._dispatch(
            "get_storage_summary", {"knowledge_base_id": knowledge_base_id}, tenant_id
        )
        return res.get("data")

    async def get_storage_documents(
        self, tenant_id: str, *, knowledge_base_id: str,
        page: int = 1, page_size: int = 20, keyword: str = "", status: str = "",
        doc_id: str = "", file_path: str = "",
    ) -> Optional[dict]:
        """文档列表（分页+筛选）。对应 action get_storage_documents。"""
        res = await self._dispatch("get_storage_documents", {
            "knowledge_base_id": knowledge_base_id, "page": page, "page_size": page_size,
            "keyword": keyword, "status": status, "doc_id": doc_id, "file_path": file_path,
        }, tenant_id)
        return res.get("data")

    async def get_storage_entities(
        self, tenant_id: str, *, knowledge_base_id: str,
        page: int = 1, page_size: int = 20, keyword: str = "", entity_type: str = "",
        doc_id: str = "", file_path: str = "",
    ) -> Optional[dict]:
        """实体列表（分页+筛选）。对应 action get_storage_entities。"""
        res = await self._dispatch("get_storage_entities", {
            "knowledge_base_id": knowledge_base_id, "page": page, "page_size": page_size,
            "keyword": keyword, "entity_type": entity_type, "doc_id": doc_id, "file_path": file_path,
        }, tenant_id)
        return res.get("data")

    async def get_storage_relationships(
        self, tenant_id: str, *, knowledge_base_id: str,
        page: int = 1, page_size: int = 20, keyword: str = "",
        doc_id: str = "", file_path: str = "",
    ) -> Optional[dict]:
        """关系列表（分页+筛选）。对应 action get_storage_relationships。"""
        res = await self._dispatch("get_storage_relationships", {
            "knowledge_base_id": knowledge_base_id, "page": page, "page_size": page_size,
            "keyword": keyword, "doc_id": doc_id, "file_path": file_path,
        }, tenant_id)
        return res.get("data")

    async def get_storage_graph_summary(
        self, tenant_id: str, *, knowledge_base_id: str,
        doc_id: str = "", file_path: str = "",
    ) -> Optional[dict]:
        """图统计。对应 action get_storage_graph_summary。"""
        res = await self._dispatch("get_storage_graph_summary", {
            "knowledge_base_id": knowledge_base_id, "doc_id": doc_id, "file_path": file_path,
        }, tenant_id)
        return res.get("data")

    async def get_storage_graph(
        self, tenant_id: str, *, knowledge_base_id: str,
        limit: int = 100, keyword: str = "", doc_id: str = "", file_path: str = "",
    ) -> Optional[dict]:
        """全图视图。对应 action get_storage_graph。"""
        res = await self._dispatch("get_storage_graph", {
            "knowledge_base_id": knowledge_base_id, "limit": limit,
            "keyword": keyword, "doc_id": doc_id, "file_path": file_path,
        }, tenant_id)
        return res.get("data")

    async def get_document_parse_result(
        self, tenant_id: str, *, knowledge_base_id: str, document_id: str,
    ) -> Optional[dict]:
        """单文档聚合（chunks + 实体 + 关系）。对应 action get_document_parse_result。"""
        res = await self._dispatch("get_document_parse_result", {
            "knowledge_base_id": knowledge_base_id, "document_id": document_id,
        }, tenant_id)
        return res.get("data")

    # ── 提示词配置 CRUD（[jonex] v1 退役：补 LOCAL 直连面薄转发） ──

    async def create_prompt(
        self,
        tenant_id: str,
        *,
        prompt_code: str,
        content: str,
        preset_name: str = "",
        display_name: str = "",
        description: str = "",
        category: str = "analysis",
        language: str = "zh",
    ) -> dict:
        """创建租户级 prompt 配置。对应 action create_prompt。"""
        payload = {
            "prompt_code": prompt_code, "content": content,
            "preset_name": preset_name, "display_name": display_name,
            "description": description, "category": category, "language": language,
        }
        return self._unwrap(
            await self._dispatch("create_prompt", payload, tenant_id), "create_prompt",
        ) or {}

    async def update_prompt(
        self, tenant_id: str, prompt_id: str, *, content: Optional[str] = None, **fields
    ) -> dict:
        """更新 prompt 配置（一般只传 content）。对应 action update_prompt。"""
        payload: dict = {"prompt_id": prompt_id, **fields}
        if content is not None:
            payload["content"] = content
        return self._unwrap(
            await self._dispatch("update_prompt", payload, tenant_id), "update_prompt",
        ) or {}

    async def delete_prompt(self, tenant_id: str, prompt_id: str) -> dict:
        """删除 prompt 配置（handler 幂等：不存在也 success，deleted=false）。"""
        return self._unwrap(
            await self._dispatch("delete_prompt", {"prompt_id": prompt_id}, tenant_id),
            "delete_prompt",
        ) or {}

    async def get_prompt(self, tenant_id: str, prompt_id: str) -> Optional[dict]:
        """按 id 查 prompt 配置；不存在返回 None（handler success=False 时回传 data）。"""
        res = await self._dispatch("get_prompt", {"prompt_id": prompt_id}, tenant_id)
        return res.get("data")

    @staticmethod
    def _unwrap(res: dict, action: str) -> Optional[dict]:
        if not res.get("success"):
            raise CapabilityInvokeError(
                message=translate("err.rag.v2_invoke_failed", params={"action": action}, fallback=f"RAG[{action}] 调用失败"),
                details={"action": action, "code": res.get("code"), "upstream_message": res.get("message")},
            )
        return res.get("data")


__all__ = ["LightRAGAdapterV2"]
