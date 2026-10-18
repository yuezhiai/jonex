# jonex_core/capability/atomic/ontology_compile/capability.py
# -*- coding: utf-8 -*-
"""[jonex] 本体编译能力（atomic.ontology_compile.v1）—— 独立容器入口。

⚠️ **能力 ID 里的名字是 `ontology_compile`，而模块路径是 `...atomic.ontology_compile`**：
`deploy/start_capability.py` 用 `CAPABILITY_NAME=ontology_compile` 拼模块与类名，
因此**本模块必须定义 `OntologyCompileCapability`**，且 `__init__.py` 不得遮蔽它。
"""
from __future__ import annotations

import logging
import uuid

from jonex_core.capability.base import BaseCapability
from jonex_core.capability.models import CapabilityMetadata, CapabilityResponse, CapabilityType

from .task_model import TaskStatus
from .task_store import get_task_store
from .worker import CompileWorker

logger = logging.getLogger(__name__)

#: 「待跑」状态 —— 与 worker 的取任务判据**必须同值**（见 Task 1 Step 3 的 ⚠️）
QUEUED = TaskStatus.QUEUED.value

#: 允许 `retry_compile` 重投的状态 —— **只允许终态**（T3 Minor② 的处置）。
#:
#: 为什么必须挡在途任务：`retry_compile` 把状态置回 `queued`，而 worker 的
#: `claim_one` **只看 `status='queued'`**。对一条 `processing` 的任务重投 ⇒ 置回 queued
#: ⇒ 可能被**再认领一次**：同一任务跑两遍（两条 worker 同时抽同一个文档、后一个的
#: `delete + 重写` 与前一个并发交错）。这不是「多跑一次无害」——写图是有副作用的。
#: 挡掉之后语义变成「重投 = 重跑一条**跑完了/失败了**的编译」，与设计 §4.2 一致。
RETRYABLE_STATUSES = frozenset({
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
})


def _extract_compiled_schema(raw):
    """从 `compile` 入参的 `schema` 里取出 compiled schema；**两种形状都收**。

    为什么两种都收（2026-10-17 真机验收发现，非推测）：

    - 设计 §4.2 的契约写作 `"schema": { "compiled_schema": {...} }` —— **嵌套**形状；
    - 而 KB 侧 `OntologyCompiler.get_compiled_schema()` 返回的是
      `OntologyCompiledSchema.to_dict()`，即 `entity_types` / `relation_types` /
      `schema_version` 都在**顶层**的**扁平**形状，`retry_extract` 也确实是原样传下来的。
      （同一段设计的注释又写「同现状 `task.ontology_schema`」，而现状就是扁平的 ——
      契约文本自身两可。）

    只认一种的失效模式是**静默的**：取不到就是 `None` ⇒ `service.run_ontology_only`
    的 `if not task.ontology_schema` 直接判 `CONFIG_INVALID`，编译任务全数失败，
    而报错只说「缺少 compiled schema」，看不出是形状问题。真机上两条样本（不同链）都栽在这上面。故此处不赌一边，两种形状都认，取不到仍返回 `None`
    （保持原有「没有 schema 就该失败」的语义）。
    """
    if not isinstance(raw, dict):
        return None
    if "compiled_schema" in raw:
        # 包装层**出现了**这个键、但值不是 dict（含 `None` / 字符串）⇒ **fail-closed**，
        # 直接返回 None，**不能**回落到下面 `raw or None` 把包装层当扁平 schema。
        #
        # 两种失效形态（后者是静默退化，比响亮失败糟得多）：
        # - `{"compiled_schema": "…"}`：回落会返回**真值**的包装层 ⇒ 绕过
        #   `service.run_ontology_only` 的 `if not task.ontology_schema` 判据，
        #   拿一个字符串/包装层当 tbox。
        # - `{"compiled_schema": None}`：旧行为返回 None（CONFIG_INVALID 响亮失败）；
        #   回落则得到 `tbox=None` ⇒ 抽取 `ok=False` ⇒ 任务 `completed` 但
        #   `ontology_status=failed`、**空产物**，而对账又会按「空产物 READY」收尾。
        #
        # 正常扁平 schema **不含** `compiled_schema` 这个键（`OntologyCompiledSchema.to_dict()`
        # 的键是 entity_types / relation_types / schema_version），故收紧无兼容风险。
        nested = raw["compiled_schema"]
        return nested if isinstance(nested, dict) else None
    return raw or None


def _coerce_generation(value) -> int:
    """把入参 `content_generation` 收敛成整数；缺省 / 脏值一律 0。

    0 = 「未提供」，与 rag 链 `retry_ontology_extract(content_generation=0)` 的缺省同值
    （KB 对账的代次栅栏据此判「没有代次信息」，而不是把一个坏值当成某个代次）。
    不抛异常：入队这一步不该因为一个整形参数而 500 —— 那会把「编译入队」变成
    「调用方参数校验器」，而真正的判据（代次）由 KB 侧在提交时就定好了。
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class OntologyCompileCapability(BaseCapability):
    """编译能力：只做入队 / 查状态 / 重投；真正的执行在 worker 里。"""

    def __init__(self) -> None:
        super().__init__()                      # 必须：registry.register 会读 _metadata
        self._store = get_task_store()
        self._worker = CompileWorker(self._store)

    def _build_metadata(self) -> CapabilityMetadata:
        return CapabilityMetadata(
            capability_id="ontology_compile",
            capability_name="Ontology Compile",
            capability_type=CapabilityType.ATOMIC,
            version="v1",
            description="本体编译：灌 LightRAG + 表格重建 + 本体抽取 + custom-kg 回写",
        )

    async def initialize(self) -> None:
        await self._worker.start()

    async def shutdown(self) -> None:
        await self._worker.stop()

    async def validate_input(self, request) -> bool:
        payload = request.payload or {}
        return bool(payload.get("action"))

    async def execute(self, request) -> CapabilityResponse:
        try:
            return await self._dispatch(request)
        except Exception as exc:  # noqa: BLE001 — 能力边界统一兜底
            logger.exception("编译能力执行失败 request=%s", request.request_id)
            return CapabilityResponse.error(request.request_id, 500, str(exc))

    async def _dispatch(self, request) -> CapabilityResponse:
        payload = request.payload or {}
        action = payload.get("action")
        data = dict(payload.get("data") or {})          # ⚠️ 业务参数在 data 里（平台既有约定）
        kb_id = str(payload.get("knowledge_base_id") or data.get("kb_id") or "")
        if action == "compile":
            return await self._compile(request, data, kb_id)
        if action == "get_compile_status":
            return await self._get_status(request, data)
        if action == "retry_compile":
            return await self._retry(request, data)
        return CapabilityResponse.error(request.request_id, 400, f"未知 action: {action}")

    async def _compile(self, request, data: dict, kb_id: str) -> CapabilityResponse:
        from .task_model import CompileTask

        compile_id = data.get("compile_id") or uuid.uuid4().hex
        task = CompileTask(
            task_id=compile_id,
            tenant_id=request.tenant_id or "",
            kb_id=kb_id,
            document_id=data.get("doc_id") or data.get("document_id") or "",
            name=data.get("file_name") or "",
            file_path=data.get("file_path") or "",
            status=QUEUED,
            ontology_schema=_extract_compiled_schema(data.get("schema")),
            config_snapshot={
                "mode": data.get("mode") or "ingest_extract",
                "options": data.get("options") or {},
            },
            # 过渡兼容：任务参数带 chunks 时优先用之（P4 后删）
            engine_chunks=data.get("chunks"),
            # 提交时的文档代次（fencing）：**必须当场记下来**，随 `get_compile_status`
            # 回显给 KB 对账的代次栅栏。见 `CompileTask.content_generation` 的注释 ——
            # 缺它则「重新解析过的文档」走新服务必判旧代次、永不 READY。
            content_generation=_coerce_generation(data.get("content_generation")),
        )
        await self._store.save(task)
        return CapabilityResponse.ok(request.request_id, data={"compile_id": compile_id, "status": QUEUED})

    async def _get_status(self, request, data: dict) -> CapabilityResponse:
        compile_id = data.get("compile_id") or data.get("id")
        task = await self._store.get(compile_id) if compile_id else None
        if task is None:
            return CapabilityResponse.error(request.request_id, 404, f"compile 任务不存在: {compile_id}")
        out = {
            "compile_id": task.task_id,
            "status": task.status,
            "stage": task.current_step or "",
            "progress": task.progress,
            # ── 旧读法兼容（KB 对账 / 看门狗读这三个字段）──
            "ontology_status": task.ontology_status,
            "ontology_error": task.ontology_error or "",
            # 提交时的文档代次：KB 对账 `_handle_completed` 的**代次栅栏**读的就是这个键
            # （与 `doc.content_generation` 比，小则判旧结果 ⇒ 接管行在途就 skip ⇒
            # 文档永不 READY）。缺键等于 0，故它不是「可选附带信息」而是承重字段。
            "content_generation": task.content_generation,
        }
        if data.get("include_data"):
            out["ontology_data"] = task.ontology_data
        return CapabilityResponse.ok(request.request_id, data=out)

    async def _retry(self, request, data: dict) -> CapabilityResponse:
        compile_id = data.get("compile_id") or data.get("id")
        task = await self._store.get(compile_id) if compile_id else None
        if task is None:
            return CapabilityResponse.error(request.request_id, 404, f"compile 任务不存在: {compile_id}")
        # ── 状态前置校验（T3 Minor②）──
        # 只允许对**终态**重投：对 queued/processing 重投会被 `claim_one` 再认领一次
        # （同一任务跑两遍），详见 `RETRYABLE_STATUSES` 的注释。`queued` 也一并挡：
        # 「已经是待跑」不需要再置一次，返回成功会掩盖「你以为重投了、其实没动」。
        # `force` 不参与这条判据：容器侧至今没有它的消费者（见 client 的契约注记），
        # 给它开门是另一项设计决定，不在本次范围内。
        status = str(getattr(task.status, "value", task.status) or "")
        if status not in RETRYABLE_STATUSES:
            return CapabilityResponse.error(
                request.request_id, 400,
                f"compile 任务当前是 {status or '未知'}，只有终态"
                f"（{'/'.join(sorted(RETRYABLE_STATUSES))}）可重投: {compile_id}",
            )
        task.status = QUEUED
        task.error_message = None
        await self._store.save(task)
        return CapabilityResponse.ok(request.request_id, data={"compile_id": compile_id, "status": QUEUED})
