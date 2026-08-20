#!/usr/bin/env python3
"""
atomic-rag-server v2 — 新 TaskManager + /invoke 统一入口

对接悦溪 Sidecar 协议:
  POST /invoke  {capability_id, payload: {action, ...}, tenant_id}
    → ActionRegistry → 新 TaskManager (raganything.service)

对比 v1 (atomic-rag-server.py):
  - 新 TaskManager（Semaphore + 协作取消 + 进度追踪 + 多租户隔离）
  - 支持 preset 解析（文档/音频/视频三种预设）
  - 支持 ModelRegistry + 多 Driver（Anthropic / OpenAI / Gemini）
  - /invoke 端点兼容 Sidecar 协议，不吃 action 注册的隐式耦合

启动:
  python atomic-rag-server-v2.py --port 8004 --profile dev
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import yaml
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import string

from raganything.service.models import CreateTaskRequest, TaskMode
from raganything.service.prompt_config_manager import (
    PromptConfigCreate,
    PromptConfigUpdate,
)

# ---------------------------------------------------------------------------
# auto-escape helper — preserves {placeholder} while escaping literal { }
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(
    r"\{([a-zA-Z_][a-zA-Z0-9_]*)(\.[a-zA-Z_][a-zA-Z0-9_]*|\[[0-9]+\])*\}",
)


def _auto_escape_braces(content: str, allowed_placeholders: set[str]) -> str:
    """Auto-escape literal { } to {{ }} while preserving {ident}-style placeholders.

    Any brace-enclosed string whose inner name matches *allowed_placeholders*
    (or the generic ``ident`` / ``ident.attr`` / ``ident[idx]`` pattern) is
    preserved; all other braces are escaped via doubling.
    """
    sentinel_map: dict[str, str] = {}
    counter = 0

    def _save(m: re.Match) -> str:
        nonlocal counter
        inner = m.group(1)
        if inner not in allowed_placeholders:
            # Not a known placeholder — leave as-is; will be escaped below
            return m.group(0)
        key = f"\x00PH\x00{counter}\x00"
        counter += 1
        sentinel_map[key] = m.group(0)
        return key

    content = _PLACEHOLDER_RE.sub(_save, content)
    content = content.replace("{", "{{").replace("}", "}}")
    for key, original in sentinel_map.items():
        content = content.replace(key, original)
    return content


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("atomic-rag-server-v2")


# ---------------------------------------------------------------------------
# Action 注册表（轻量，不依赖旧 atomic_rag/actions/）
# ---------------------------------------------------------------------------
class ActionRegistry:
    """独立注册表 — 不与旧 atomic_rag.actions.ActionRegistry 耦合."""

    _handlers: dict = {}

    @classmethod
    def register(cls, name: str):
        def decorator(f):
            cls._handlers[name] = f
            return f
        return decorator

    @classmethod
    def get(cls, name: str):
        return cls._handlers.get(name)

    @classmethod
    def actions(cls) -> list[str]:
        return list(cls._handlers.keys())


# ---------------------------------------------------------------------------
# 请求模型（兼容 Sidecar 协议）
# ---------------------------------------------------------------------------
# 被禁用的无效租户 ID 占位值集合
_FORBIDDEN_TENANTS: frozenset = frozenset({"default", "default_tenant", "system", ""})


class InvokeRequest(BaseModel):
    capability_id: str = ""
    payload: dict = {}
    tenant_id: str = ""   # Sidecar 必须传入真实租户；空字符串/占位值将触发 reject
    user_id: str | None = None
    request_id: str | None = None


# ---------------------------------------------------------------------------
# [jonex] R1：幂等键公共匹配函数 —— 遍历 _tasks（从 repo 恢复）按 idempotency_key
# 匹配在途任务；找到非终态任务则复用，否则创建新任务。比 _idempotency 内存字典更可靠
#（后者随容器重启丢失）。insert/retry/retry_ontology_extract 三处共用。
# ---------------------------------------------------------------------------

async def _reuse_or_create(task_manager, idempotency_key: str | None,
                           req, tenant_id: str) -> tuple:
    """按幂等键查找或用 req 创建任务。返回 (task_id, status, is_reused: bool)。"""
    from raganything.service.models import TERMINAL_STATES

    if idempotency_key:
        for t in task_manager._tasks.values():
            if t.idempotency_key != idempotency_key:
                continue
            if t.status in TERMINAL_STATES:
                continue  # 终态允许新建 attempt
            # 非终态在途任务 → 复用
            return (t.task_id, t.status.value, True)

    result = await task_manager.create(req, tenant_id, idempotency_key=idempotency_key)
    task_id = result.task_id if hasattr(result, "task_id") else ""
    return (task_id, "pending", False)


# ---------------------------------------------------------------------------
# Action handlers — 全部走新 TaskManager
# ---------------------------------------------------------------------------

@ActionRegistry.register("insert")
async def handle_insert(params: dict, tenant_id: str, task_manager, **kwargs):
    """创建解析任务 → 新 TaskManager.create()"""
    file_path = params.get("file_path", "") or None
    mps_video_url = params.get("mps_video_url", "") or None
    if not file_path and not mps_video_url:
        raise HTTPException(400, "file_path 和 mps_video_url 至少传一个")

    preset = params.get("preset")
    mode = TaskMode.PRESET if preset else TaskMode.INLINE

    idempotency_key = params.get("idempotency_key")  # [jonex] R1

    req = CreateTaskRequest(
        mode=mode,
        file_path=file_path or mps_video_url or "unknown",
        preset=preset,
        output_dir=params.get("output_dir"),
        profile=params.get("profile"),
        # 不透传 modalities：None 会触发 pydantic 校验失败（list[str] 不接受 None）。
        # 不设则用 default_factory；且 PRESET 模式下 model_fields_set 不含 modalities，
        # ConfigResolver._extract_overrides 会剔除它 → 采用 preset yaml 的 modalities。
        **({"modalities": params["modalities"]} if params.get("modalities") is not None else {}),
        webhook_url=params.get("webhook_url"),
        llm=params.get("llm"),
        embedding=params.get("embedding"),
        vlm=params.get("vlm"),
        asr=params.get("asr"),
        llm_host=params.get("llm_host"),
        llm_api_key=params.get("llm_api_key"),
        vlm_host=params.get("vlm_host"),
        vlm_api_key=params.get("vlm_api_key"),
        embedding_host=params.get("embedding_host"),
        force_reparse=params.get("force_reparse", False),
        # KB integration fields
        kb_id=params.get("knowledge_base_id", ""),
        knowledge_base_id=params.get("knowledge_base_id"),
        document_id=params.get("document_id"),
        storage_backend=params.get("storage_backend", "local"),
        storage_key=params.get("storage_key"),
        mps_video_url=mps_video_url or params.get("mps_video_url") or "",
        ontology_schema=params.get("ontology_schema") or None,
        # [jonex] 主解析提示词下发（KB 关联的 prompt 配置 id）
        prompt_ids=params.get("prompt_ids") or [],
        # ── Reparse / recompile execution control ──
        # [jonex] OpenKB：透传 execution_mode（parse_only 时只解析、不入库/不抽本体）
        execution_mode=params.get("execution_mode", "full") or "full",
        schema_version=int(params.get("schema_version", 0) or 0),
        schema_hash=params.get("schema_hash", "") or "",
    )

    # [jonex] R1：幂等键复用 —— 同一 (doc, generation) 返回同一 task_id
    task_id, status, is_reused = await _reuse_or_create(
        task_manager, idempotency_key, req, tenant_id,
    )

    return {
        "success": True, "code": 0, "message": "success",
        "data": {"task_id": task_id, "status": status,
                 "file_path": file_path, "tenant_id": tenant_id,
                 "reused": is_reused},  # [jonex] R1：告知 kb 侧是否为复用
    }


@ActionRegistry.register("query")
async def handle_query(params: dict, tenant_id: str, task_manager, **kwargs):
    """检索 → 新 TaskManager.query()"""
    query_text = params.get("query", "")
    if not query_text.strip():
        raise HTTPException(400, "query 不能为空")

    result = await task_manager.query(
        query=query_text, tenant_id=tenant_id,
        mode=params.get("mode", "hybrid"),
        top_k=int(params.get("top_k", 5)),
        kb_id=params.get("knowledge_base_id", ""),
    )
    return {"success": True, "code": 0, "message": "success",
            "data": {"answer": result["answer"], "references": result["references"]}}


@ActionRegistry.register("delete")
async def handle_delete(params: dict, tenant_id: str, task_manager, **kwargs):
    """删除文档 → 新 TaskManager.delete_doc()"""
    doc_id = params.get("doc_id", "")
    if not doc_id:
        raise HTTPException(400, "doc_id 不能为空")

    success = await task_manager.delete_doc(
        doc_id=doc_id, tenant_id=tenant_id,
        kb_id=params.get("knowledge_base_id", ""),
    )
    return {"success": True, "code": 0, "message": "success",
            "data": {"success": success}}


@ActionRegistry.register("delete_batch")
async def handle_delete_batch(params: dict, tenant_id: str, task_manager, **kwargs):
    """[jonex] 批量删除——单次提交全部 doc_ids，LightRAG 仅执行一次 rebuild。"""
    doc_ids = params.get("doc_ids", [])
    if not doc_ids or not isinstance(doc_ids, list):
        raise HTTPException(400, "doc_ids 不能为空且必须是数组")

    result = await task_manager.delete_docs(
        doc_ids=doc_ids, tenant_id=tenant_id,
        kb_id=params.get("knowledge_base_id", ""),
        document_id=params.get("document_id", ""),
        trace_id=params.get("trace_id", ""),
    )
    accepted = result.get("accepted", [])
    accepted_set = set(accepted)
    failed = [d for d in doc_ids if d not in accepted_set]
    return {"success": True, "code": 0, "message": "success",
            "data": {"success": len(failed) == 0,
                     "accepted": accepted,
                     "failed": failed,
                     "status": result.get("status", "")}}


@ActionRegistry.register("get_task_status")
async def handle_get_task_status(params: dict, tenant_id: str, task_manager, **kwargs):
    """查询任务状态 → 新 TaskManager.get()"""
    task_id = params.get("task_id", "")
    if not task_id:
        raise HTTPException(400, "task_id 不能为空")

    task = await task_manager.get(task_id, tenant_id)
    if task is None:
        return {"success": False, "code": 40401, "message": "task not found",
                "data": {"task_id": task_id, "status": "not_found"}}

    # ── lightrag_doc_ids: prefer dedicated field, fallback to result_summary ──
    lightrag_doc_ids: list[str] = list(task.lightrag_doc_ids)
    if not lightrag_doc_ids and task.result_summary and task.result_summary.doc_id:
        lightrag_doc_ids = [task.result_summary.doc_id]

    return {
        "success": True, "code": 0, "message": "success",
        "data": {
            "task_id": task.task_id,
            "status": task.status.value,
            "progress": task.progress,
            "error": task.error_message,
            # [jonex] P0-a: 透传 error_code，供 KB 对账 P1-5 确定性终态短路使用
            "error_code": task.error_code.value if task.error_code else None,
            "lightrag_doc_ids": lightrag_doc_ids,
            "failed_chunk_count": task.failed_chunk_count,
            "total_chunk_count": task.total_chunk_count,
            # [jonex] #6: expose timeout/duplicated classification for KB reconciliation
            "timeout_chunk_count": task.timeout_chunk_count,
            "duplicated_chunk_count": task.duplicated_chunk_count,
            "doc_id": (
                task.result_summary.doc_id
                if task.result_summary else ""
            ),
            # ── KB integration fields ──
            "kb_id": task.kb_id,
            "document_id": task.document_id,
            "ontology_status": task.ontology_status or None,
            "ontology_data": task.ontology_data,
            "ontology_error": task.ontology_error or None,
            # ── Reparse / recompile fencing 字段 ──
            "execution_mode": getattr(task, "execution_mode", "full"),
            "content_generation": getattr(task, "content_generation", 0),
            "schema_version": getattr(task, "schema_version", 0),
            "schema_hash": getattr(task, "schema_hash", ""),
            "current_step": task.current_step or "",
            # [jonex] 3-A：暴露 cleanup 进度，供 KB 对账按删除量算动态超时
            "cleanup_total": getattr(task, "cleanup_total", 0),
            "cleanup_pending_count": (
                len(task.delete_pending_ids or []) + len(task.compensate_pending_ids or [])
            ),
            "stage_timings": [
                {
                    "stage": s.stage,
                    "label": s.label,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                    "elapsed_seconds": s.elapsed_seconds,
                }
                for s in (task.timeline or [])
                if hasattr(s, "stage")
            ],
            "storage": (
                task.storage.model_dump(mode="json")
                if task.storage else None
            ),
            # [jonex] OpenKB parsed artifact 相对路径（供 KB→OpenKB 编译；相对 inputs 卷根）
            "parsed_markdown_path": (
                (task.result_summary.extensions or {}).get("parsed_markdown_path", "")
                if task.result_summary else ""
            ),
            "assets_dir": (
                (task.result_summary.extensions or {}).get("assets_dir", "")
                if task.result_summary else ""
            ),
            # [jonex] 多模态转写警告（失败/跳过/空内容的机器可读信号，_record_multimodal_warning 写入）
            "multimodal_warnings": (
                (task.result_summary.extensions or {}).get("multimodal_warnings", [])
                if task.result_summary else []
            ),
        },
    }


@ActionRegistry.register("get_doc_chunks")
async def handle_get_doc_chunks(params: dict, tenant_id: str, task_manager, **kwargs):
    """查看文档 chunks — 含行号/时间轴等位置元数据（按 doc_id 直查，不依赖 task）"""
    doc_id = params.get("doc_id", "")
    if not doc_id:
        raise HTTPException(400, "doc_id 不能为空")

    result = await task_manager.get_document_chunks(
        doc_id, tenant_id,
        kb_id=params.get("knowledge_base_id", ""),
    )
    if result is None:
        return {"success": False, "code": 40405, "message": "doc not found or no chunks",
                "data": {"doc_id": doc_id, "total": 0, "chunks": []}}

    return {"success": True, "code": 0, "message": "success", "data": result}


@ActionRegistry.register("get_chunk")
async def handle_get_chunk(params: dict, tenant_id: str, task_manager, **kwargs):
    """按 chunk_id 直查单个 chunk 内容（不依赖 task，不拉整篇 chunk 列表）"""
    chunk_id = params.get("chunk_id", "")
    if not chunk_id:
        raise HTTPException(400, "chunk_id 不能为空")

    result = await task_manager.get_chunk_by_id(
        chunk_id, tenant_id,
        kb_id=params.get("knowledge_base_id", ""),
    )
    if result is None:
        return {"success": False, "code": 40405, "message": "chunk not found",
                "data": None}

    return {"success": True, "code": 0, "message": "success", "data": result}


@ActionRegistry.register("export_doc")
async def handle_export_doc(params: dict, tenant_id: str, task_manager, **kwargs):
    """导出解析结果 — 聚合全文/chunks/实体/关系（按 doc_id 直查，不依赖 task）"""
    doc_id = params.get("doc_id", "")
    if not doc_id:
        raise HTTPException(400, "doc_id 不能为空")

    fmt = params.get("format", "json")
    result = await task_manager.export_document(
        doc_id, tenant_id, fmt=fmt,
        kb_id=params.get("knowledge_base_id", ""),
    )
    if result is None:
        return {"success": False, "code": 40405, "message": "doc not found or no data",
                "data": None}

    return {"success": True, "code": 0, "message": "success", "data": result}


@ActionRegistry.register("retry")
async def handle_retry(params: dict, tenant_id: str, task_manager, **kwargs):
    """重新解析 — 用 force_reparse=true 重新提交同一文件"""
    file_path = params.get("file_path", "") or None
    mps_video_url = params.get("mps_video_url", "") or None
    if not file_path and not mps_video_url:
        raise HTTPException(400, "file_path 和 mps_video_url 至少传一个")

    preset = params.get("preset")
    mode = TaskMode.PRESET if preset else TaskMode.INLINE

    idempotency_key = params.get("idempotency_key")  # [jonex] R1

    req = CreateTaskRequest(
        mode=mode,
        file_path=file_path or mps_video_url or "unknown",
        preset=preset,
        output_dir=params.get("output_dir"),
        profile=params.get("profile"),
        **({"modalities": params["modalities"]} if params.get("modalities") is not None else {}),
        webhook_url=params.get("webhook_url"),
        llm=params.get("llm"),
        embedding=params.get("embedding"),
        vlm=params.get("vlm"),
        asr=params.get("asr"),
        llm_host=params.get("llm_host"),
        llm_api_key=params.get("llm_api_key"),
        vlm_host=params.get("vlm_host"),
        vlm_api_key=params.get("vlm_api_key"),
        embedding_host=params.get("embedding_host"),
        force_reparse=True,  # retry always forces re-parse
        # KB integration fields
        kb_id=params.get("knowledge_base_id", ""),
        knowledge_base_id=params.get("knowledge_base_id"),
        document_id=params.get("document_id"),
        storage_backend=params.get("storage_backend", "local"),
        storage_key=params.get("storage_key"),
        mps_video_url=mps_video_url or params.get("mps_video_url") or "",
        ontology_schema=params.get("ontology_schema") or None,
        # [jonex] 主解析提示词下发（重解析同样带上当前配置）
        prompt_ids=params.get("prompt_ids") or [],
        # ── Reparse / recompile execution control ──
        execution_mode=params.get("execution_mode", "full") or "full",
        content_generation=int(params.get("content_generation", 0) or 0),
        strict_push=bool(params.get("strict_push", False)),
        schema_version=int(params.get("schema_version", 0) or 0),
        schema_hash=params.get("schema_hash", "") or "",
    )

    # [jonex] R1：幂等键复用
    task_id, status, is_reused = await _reuse_or_create(
        task_manager, idempotency_key, req, tenant_id,
    )

    return {
        "success": True, "code": 0, "message": "success",
        "data": {"task_id": task_id, "status": status,
                 "file_path": file_path, "tenant_id": tenant_id,
                 "force_reparse": True,
                 "reused": is_reused},  # [jonex] R1
    }


# [jonex] R1：幂等键反查 —— 供 KB 对账 R2-b 查证链使用
@ActionRegistry.register("get_task_by_idempotency_key")
async def handle_get_task_by_idempotency_key(params: dict, tenant_id: str,
                                              task_manager, **kwargs):
    """按幂等键查找在途任务，返回 {task_id, status} 或 not_found。"""
    idempotency_key = params.get("idempotency_key", "")
    if not idempotency_key:
        raise HTTPException(400, "idempotency_key 不能为空")

    for t in task_manager._tasks.values():
        if t.idempotency_key == idempotency_key:
            return {
                "success": True, "code": 0, "message": "success",
                "data": {"task_id": t.task_id, "status": t.status.value,
                         "found": True},
            }

    return {
        "success": True, "code": 0, "message": "success",
        "data": {"found": False},
    }


# ---------------------------------------------------------------------------
# 提示词配置 CRUD（KB 主解析提示词联动）— 复用 PromptConfigManager
# ---------------------------------------------------------------------------

def _allowed_placeholders(prompt_code: str) -> set[str] | None:
    """返回某 prompt_code 允许的占位符字段集合（含 base 与 _with_context 变体）。

    取自内置 PROMPTS 默认模板自身的占位符字段（单一事实来源）。返回 None 表示
    该 code 无内置模板（未知 code），调用方应放行（运行期用 blank-default 兜底）。
    """
    try:
        from raganything.prompt import PROMPTS
    except Exception:
        return None
    fields: set[str] = set()
    found = False
    for code in (prompt_code, f"{prompt_code}_with_context"):
        tmpl = PROMPTS.get(code) if hasattr(PROMPTS, "get") else None
        if not isinstance(tmpl, str):
            continue
        found = True
        try:
            for _lit, field, _spec, _conv in string.Formatter().parse(tmpl):
                if field:
                    # 只取顶层字段名（去掉 .attr / [idx]）
                    fields.add(field.split(".")[0].split("[")[0])
        except Exception:
            pass
    return fields if found else None


def _validate_prompt_template(prompt_code: str, content: str) -> str:
    """保存前校验用户 prompt 模板：括号成对 + 占位符在白名单内。违规抛 HTTPException(400)。

    权威校验点（KB 侧亦做轻量预检，最终以此为准）。
    若检测到 JSON 字面大括号（字段名非 Python 合法标识符），自动转义 literal { }
    为 {{ }}，保留已知占位符。返回最终应落库的内容（已转义，如需要）。
    """
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(400, "prompt content 不能为空")
    # 1) 括号语法 + 自动转义检测
    used: set[str] = set()
    needs_escape = False
    try:
        for _lit, field, _spec, _conv in string.Formatter().parse(content):
            if field is not None and field != "":
                # Fields like '"key"' (from JSON) are not valid Python identifiers
                if not field.isidentifier():
                    needs_escape = True
                used.add(field.split(".")[0].split("[")[0])
    except ValueError:
        needs_escape = True  # unbalanced braces → try auto-escape

    if needs_escape:
        # Try auto-escape: preserve known placeholders, escape everything else
        allowed = _allowed_placeholders(prompt_code)
        content = _auto_escape_braces(content, allowed or set())
        used = set()
        try:
            for _lit, field, _spec, _conv in string.Formatter().parse(content):
                if field is not None and field != "":
                    used.add(field.split(".")[0].split("[")[0])
        except ValueError as e:
            raise HTTPException(
                400,
                f"prompt 模板大括号非法：{e}。字面大括号请写成 {{{{ }}}}，占位符仅限允许字段。",
            )

    # 2) 占位符白名单
    allowed = _allowed_placeholders(prompt_code)
    if allowed is not None:
        illegal = {f for f in used if f and f not in allowed}
        if illegal:
            raise HTTPException(
                400,
                f"prompt 含不允许的占位符 {sorted(illegal)}；{prompt_code} 允许："
                f"{sorted(allowed) or '（无占位符）'}",
            )
    return content


def _require_pcm(kwargs: dict):
    pcm = kwargs.get("prompt_config_manager")
    if pcm is None:
        raise HTTPException(500, "prompt_config_manager not available")
    return pcm


@ActionRegistry.register("create_prompt")
async def handle_create_prompt(params: dict, tenant_id: str, task_manager, **kwargs):
    """创建租户级 prompt 配置（覆盖内置 PROMPTS 某个 code）。"""
    pcm = _require_pcm(kwargs)
    prompt_code = params.get("prompt_code", "")
    content = params.get("content", "")
    if not prompt_code:
        raise HTTPException(400, "prompt_code 不能为空")
    content = _validate_prompt_template(prompt_code, content)

    item = pcm.create(
        tenant_id,
        PromptConfigCreate(
            prompt_code=prompt_code,
            content=content,
            preset_name=params.get("preset_name", "") or "",
            display_name=params.get("display_name", "") or "",
            description=params.get("description", "") or "",
            category=params.get("category", "analysis") or "analysis",
            language=params.get("language", "zh") or "zh",
        ),
        created_by=params.get("created_by", "kb") or "kb",
    )
    return {"success": True, "code": 0, "message": "success", "data": item.model_dump(mode="json")}


@ActionRegistry.register("update_prompt")
async def handle_update_prompt(params: dict, tenant_id: str, task_manager, **kwargs):
    """更新 prompt 配置（一般只传 content）。未找到 → 40404。"""
    pcm = _require_pcm(kwargs)
    prompt_id = params.get("prompt_id", "")
    if not prompt_id:
        raise HTTPException(400, "prompt_id 不能为空")

    # 若改了 content / prompt_code，做模板校验（用最终 code）
    content = params.get("content")
    if content is not None:
        existing = pcm.get(tenant_id, prompt_id)
        code = params.get("prompt_code") or (existing.prompt_code if existing else "")
        if code:
            content = _validate_prompt_template(code, content)

    fields = {}
    for k in ("prompt_code", "preset_name", "display_name", "description",
              "category", "language", "content"):
        if params.get(k) is not None:
            fields[k] = params[k]
    # Use the potentially-escaped content
    if content is not None:
        fields["content"] = content

    item = pcm.update(
        tenant_id, prompt_id, PromptConfigUpdate(**fields),
        updated_by=params.get("updated_by", "kb") or "kb",
    )
    if item is None:
        return {"success": False, "code": 40404, "message": f"prompt not found: {prompt_id}", "data": None}
    return {"success": True, "code": 0, "message": "success", "data": item.model_dump(mode="json")}


@ActionRegistry.register("delete_prompt")
async def handle_delete_prompt(params: dict, tenant_id: str, task_manager, **kwargs):
    """删除 prompt 配置。幂等：不存在也返回 success（deleted=false），不报 404。"""
    pcm = _require_pcm(kwargs)
    prompt_id = params.get("prompt_id", "")
    if not prompt_id:
        raise HTTPException(400, "prompt_id 不能为空")
    deleted = pcm.delete(tenant_id, prompt_id)
    return {"success": True, "code": 0, "message": "success", "data": {"deleted": bool(deleted)}}


@ActionRegistry.register("get_prompt")
async def handle_get_prompt(params: dict, tenant_id: str, task_manager, **kwargs):
    """按 id 查 prompt 配置（对账探测悬空 id 用）。未找到 → 40404。"""
    pcm = _require_pcm(kwargs)
    prompt_id = params.get("prompt_id", "")
    if not prompt_id:
        raise HTTPException(400, "prompt_id 不能为空")
    item = pcm.get(tenant_id, prompt_id)
    if item is None:
        return {"success": False, "code": 40404, "message": f"prompt not found: {prompt_id}", "data": None}
    return {"success": True, "code": 0, "message": "success", "data": item.model_dump(mode="json")}


@ActionRegistry.register("update_chunk")
async def handle_update_chunk(params: dict, tenant_id: str, task_manager, **kwargs):
    """修改 chunk 内容 — 经 :9621 /documents/chunks/update 端点。

    服务端事务性执行：upsert new chunk → delete old chunk → rewrite entity/relation references。
    """
    http_client = getattr(task_manager, "_http_client", None)
    if http_client is None:
        raise HTTPException(500, "HTTP client not available")

    chunk_id = params.get("chunk_id", "")
    new_content = params.get("new_content", "")
    if not chunk_id:
        raise HTTPException(400, "chunk_id 不能为空")
    if not new_content:
        raise HTTPException(400, "new_content 不能为空")

    kb_id = params.get("knowledge_base_id", "")
    expected_hash = params.get("expected_content_hash")

    try:
        result = await http_client.update_chunk(
            old_chunk_id=chunk_id,
            new_content=new_content,
            tenant_id=tenant_id,
            kb_id=kb_id,
            file_source=params.get("file_source", ""),
            expected_content_hash=expected_hash,
        )
        return {"success": True, "code": 0, "message": "success", "data": result}
    except Exception as e:
        code = getattr(e, "code", 500)
        return {"success": False, "code": code, "message": str(e), "data": None}


@ActionRegistry.register("delete_orphan_chunk")
async def handle_delete_orphan_chunk(params: dict, tenant_id: str, task_manager, **kwargs):
    """[jonex] O7：孤儿 chunk 强制清理 — 经 :9621 DELETE /documents/chunks/{chunk_id}。

    reparse 删除未收敛的残留 chunk 在 doc_status 无记录，doc 级删除会
    not found 直接返回；本 action 按 chunk_id 直删 text_chunks/向量/full_docs。
    """
    http_client = getattr(task_manager, "_http_client", None)
    if http_client is None:
        raise HTTPException(500, "HTTP client not available")

    chunk_id = params.get("chunk_id", "")
    if not chunk_id:
        raise HTTPException(400, "chunk_id 不能为空")
    kb_id = params.get("knowledge_base_id", "")

    try:
        ok = await http_client.delete_orphan_chunk(
            chunk_id, tenant_id=tenant_id, kb_id=kb_id,
        )
        return {
            "success": True, "code": 0, "message": "success",
            "data": {"chunk_id": chunk_id, "deleted": ok},
        }
    except Exception as e:
        code = getattr(e, "code", 500)
        return {"success": False, "code": code, "message": str(e), "data": None}


@ActionRegistry.register("upsert_preset")
async def handle_upsert_preset(params: dict, tenant_id: str, task_manager, **kwargs):
    """创建/更新 preset — 支持 tenant/kb 作用域。

    参数:
        preset_name:   预设名称（必填）
        description:   描述
        version:       版本号
        config:        配置 dict（必填）
        kb_id:         知识库 ID（可选，有则写 kb 级目录）
    """
    config_resolver = kwargs.get("config_resolver")
    if config_resolver is None:
        raise HTTPException(500, "config_resolver not available")

    preset_name = params.get("preset_name", "")
    if not preset_name:
        raise HTTPException(400, "preset_name 不能为空")
    config_data = params.get("config")
    if not config_data or not isinstance(config_data, dict):
        raise HTTPException(400, "config 不能为空且必须是 dict")

    kb_id = params.get("kb_id", "")
    base = config_resolver._presets_dir

    # 确定作用域目录
    if tenant_id and kb_id:
        presets_dir = base.parent / tenant_id / kb_id / "presets"
        scope = "kb"
    elif tenant_id:
        presets_dir = base.parent / tenant_id / "presets"
        scope = "tenant"
    else:
        presets_dir = base
        scope = "global"

    presets_dir.mkdir(parents=True, exist_ok=True)
    path = presets_dir / f"{preset_name}.yaml"

    description = params.get("description", "")
    version = params.get("version", "1.0")
    updated_by = params.get("updated_by", "sidecar")

    # Helper: YAML-safe inline string (strips trailing document-end marker)
    def _yaml_str(val: str) -> str:
        dumped = yaml.safe_dump(val, allow_unicode=True, default_flow_style=True)
        if dumped.endswith("...\n"):
            dumped = dumped[:-4]
        elif dumped.endswith("..."):
            dumped = dumped[:-3]
        return dumped.strip()

    # Write YAML manually to preserve key order: metadata → config last
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(f"description: {_yaml_str(description)}\n")
        f.write(f"version: {_yaml_str(version)}\n")
        f.write(f"updated_at: {_yaml_str(datetime.now(timezone.utc).isoformat())}\n")
        f.write(f"updated_by: {_yaml_str(updated_by)}\n")
        f.write(f"scope: {scope}\n")
        f.write(f"tenant_id: {_yaml_str(tenant_id)}\n")
        if kb_id:
            f.write(f"kb_id: {_yaml_str(kb_id)}\n")
        f.write("config:\n")
        config_yaml = yaml.safe_dump(
            config_data, allow_unicode=True, default_flow_style=False, sort_keys=False,
        )
        for line in config_yaml.split("\n"):
            f.write(f"  {line}\n" if line.strip() else "\n")
    os.replace(tmp, path)

    return {
        "success": True, "code": 0, "message": "success",
        "data": {
            "name": preset_name, "scope": scope,
            "tenant_id": tenant_id, "kb_id": kb_id,
            "path": str(path),
        },
    }


@ActionRegistry.register("retry_ontology_extract")
async def handle_retry_ontology_extract(params: dict, tenant_id: str, task_manager, **kwargs):
    """重新触发本体抽取（ontology-only）— 只重抽本体，跳过 parse/push/文件校验。

    [jonex] P0-A：走 execution_mode=ontology_only；幂等键含 tenant/kb/schema_version，
    只复用非终态任务，failed/cancelled/completed 允许新建 attempt（completed 由 force_retry 决定）。
    """
    from raganything.service.models import TERMINAL_STATES

    document_id = params.get("document_id", "")
    kb_id = params.get("knowledge_base_id", "")
    file_path = params.get("file_path", "")
    ontology_schema = params.get("ontology_schema") or None
    schema_version = int(params.get("schema_version", 0) or 0)
    schema_hash = params.get("schema_hash", "") or ""
    force_retry = bool(params.get("force_retry", False))
    content_generation = int(params.get("content_generation", 0) or 0)

    if not document_id:
        raise HTTPException(400, "document_id 不能为空")
    # ontology-only 必须携带 compiled schema，否则无法归类
    if not ontology_schema:
        raise HTTPException(400, "ontology_only 缺少 ontology_schema（compiled schema）")

    # 版本兜底 hash（用于同版本号下内容变更的区分；schema_version 为主键组件）
    if not schema_hash:
        schema_hash = hashlib.md5(
            json.dumps(ontology_schema, sort_keys=True).encode()
        ).hexdigest()[:12]

    # 幂等键：tenant + kb + document + schema_version（P0-A.5）
    idempotency_key = f"ontology:{tenant_id}:{kb_id}:{document_id}:{schema_version}"

    # 只复用同 key 的非终态任务；终态（failed/cancelled/completed）允许新建 attempt。
    # completed 是否复用由 force_retry 决定。
    for t in task_manager._tasks.values():
        if t.idempotency_key != idempotency_key:
            continue
        if t.status in TERMINAL_STATES:
            if t.status.value == "completed" and not force_retry:
                return {
                    "success": True, "code": 0, "message": "success",
                    "data": {"status": "completed", "task_id": t.task_id},
                }
            continue  # failed/cancelled 或 force_retry → 允许新建
        # 非终态在途任务 → 复用
        return {
            "success": True, "code": 0, "message": "success",
            "data": {"status": t.status.value, "task_id": t.task_id},
        }

    req = CreateTaskRequest(
        mode=TaskMode.INLINE,
        file_path=file_path,
        kb_id=kb_id,
        knowledge_base_id=kb_id,
        ontology_schema=ontology_schema,
        document_id=document_id,
        execution_mode="ontology_only",
        schema_version=schema_version,
        schema_hash=schema_hash,
        force_retry=force_retry,
        content_generation=content_generation,
    )
    result = await task_manager.create(req, tenant_id, idempotency_key=idempotency_key)
    task_id = result.task_id if hasattr(result, "task_id") else ""

    return {
        "success": True, "code": 0, "message": "success",
        "data": {"status": "queued", "task_id": task_id},
    }


# ── Storage-reader handler factory ────────────────────────────────────────


def _make_storage_handler(action_name: str):
    async def handler(params: dict, tenant_id: str, task_manager, **kwargs):
        http_client = getattr(task_manager, "_http_client", None)
        if http_client is None:
            raise HTTPException(500, "HTTP client not available")

        kb_id = params.get("knowledge_base_id", "")
        page = int(params.get("page", 1))
        page_size = int(params.get("page_size", 20))
        keyword = params.get("keyword", "")
        doc_id = params.get("doc_id", "")
        file_path = params.get("file_path", "")

        try:
            if action_name == "get_storage_summary":
                data = await http_client.get_summary(tenant_id, kb_id)
            elif action_name == "get_storage_documents":
                data = await http_client.get_documents(
                    tenant_id, kb_id, page=page, page_size=page_size,
                    keyword=keyword, status=params.get("status", ""),
                    document_id=doc_id, file_path=file_path)
            elif action_name == "get_storage_entities":
                data = await http_client.get_entities(
                    tenant_id, kb_id, page=page, page_size=page_size,
                    keyword=keyword, entity_type=params.get("entity_type", ""),
                    document_id=doc_id, file_path=file_path)
            elif action_name == "get_storage_relationships":
                data = await http_client.get_relationships(
                    tenant_id, kb_id, page=page, page_size=page_size,
                    keyword=keyword, document_id=doc_id, file_path=file_path)
            elif action_name == "get_storage_graph_summary":
                data = await http_client.get_graph_summary(
                    tenant_id, kb_id, document_id=doc_id, file_path=file_path)
            elif action_name == "get_storage_graph":
                data = await http_client.get_graph(
                    tenant_id, kb_id, limit=int(params.get("limit", 100)),
                    keyword=keyword, document_id=doc_id, file_path=file_path)
            elif action_name == "get_document_parse_result":
                data = await http_client.get_document_parse_result(
                    tenant_id, kb_id, document_id=params.get("document_id", ""))
            else:
                raise HTTPException(400, f"Unknown storage action: {action_name}")
            return {"success": True, "code": 0, "message": "success", "data": data}
        except Exception as e:
            code = getattr(e, "code", 500)
            return {"success": False, "code": code, "message": str(e), "data": None}

    handler.__name__ = f"handle_{action_name}"
    return handler


for _action_name in [
    "get_storage_summary", "get_storage_documents",
    "get_storage_entities", "get_storage_relationships",
    "get_storage_graph_summary", "get_storage_graph",
    "get_document_parse_result",
]:
    ActionRegistry.register(_action_name)(_make_storage_handler(_action_name))


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------
_task_manager = None   # 由 startup 初始化
_config_resolver = None


def create_app(task_manager, config_resolver, prompt_config_manager=None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # HTTP mode: init multimodal processors (VLM/ASR) on startup
        if task_manager._pipeline_executor is not None:
            await task_manager._pipeline_executor._init_processors_via_builder()
        await task_manager.start()
        yield
        await task_manager.shutdown()

    app = FastAPI(title="atomic-rag-v2", version="2.0.0", lifespan=lifespan)
    app.state.task_manager = task_manager
    app.state.config_resolver = config_resolver
    app.state.prompt_config_manager = prompt_config_manager

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "2.0.0",
                "actions": ActionRegistry.actions()}

    @app.post("/invoke")
    async def invoke(req: InvokeRequest):
        """统一能力调用入口 — Sidecar 协议兼容"""
        action = req.payload.get("action")
        tenant_id = req.tenant_id
        if not tenant_id or tenant_id in _FORBIDDEN_TENANTS:
            raise HTTPException(400, f"无效的 tenant_id: {tenant_id!r}")
        tm = app.state.task_manager
        cr = app.state.config_resolver

        handler = ActionRegistry.get(action)
        if not handler:
            return {
                "request_id": req.request_id or str(uuid.uuid4())[:12],
                "success": False, "code": 400,
                "message": f"不支持的 action: {action}",
                "data": None,
            }

        try:
            data = await handler(
                req.payload, tenant_id,
                task_manager=tm, config_resolver=cr,
                prompt_config_manager=app.state.prompt_config_manager,
            )
            return {
                "request_id": req.request_id or str(uuid.uuid4())[:12],
                **data,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"invoke {action} 失败: {e}")
            return {
                "request_id": req.request_id or str(uuid.uuid4())[:12],
                "success": False, "code": 500,
                "message": str(e), "data": None,
            }

    return app


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(description="atomic-rag-server v2")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8004)
    parser.add_argument("--profile", default="dev")
    parser.add_argument("--base-dir", default="./rag_service_data")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level.upper()))

    profiles_dir = os.path.join("config", "profiles")
    presets_dir = os.path.join("config", "presets")

    from raganything.service.model_factory import ModelFactory
    from raganything.service.task_manager import TaskManager
    from raganything.service.config_resolver import ConfigResolver
    from raganything.service.http_lightrag_client import HttpLightRagClient
    from raganything.service.prompt_config_manager import PromptConfigManager

    mf = ModelFactory(profiles_dir=profiles_dir)
    pcm = PromptConfigManager(
        base_dir=os.getenv("PROMPT_CONFIG_DIR", os.path.join(args.base_dir, "prompt_configs"))
    )

    # ── HTTP mode: assemble RAGAnything with Parser + VLM + HttpLightRagClient ──
    http_client = HttpLightRagClient()

    from raganything.raganything import RAGAnything
    from raganything.config import RAGAnythingConfig
    from raganything.parsers import get_parser

    parser = get_parser(os.getenv("RAG_PARSER", "mineru"))
    vlm_result = mf.build_vlm()
    vlm_func = vlm_result["func"] if vlm_result else None
    vlm_bound = vlm_result["bound"] if vlm_result else None

    pipeline_executor = RAGAnything(
        config=RAGAnythingConfig(working_dir=args.base_dir),
        vlm_model_func=vlm_func,
        vlm_bound=vlm_bound,
        llm_model_func=None,         # :9621 handles LLM internally
        embedding_func=None,         # :9621 handles embedding internally
        http_client=http_client,
        doc_parser=parser,
    )
    tm = TaskManager(
        base_dir=args.base_dir,
        model_factory=mf,
        http_client=http_client,
        pipeline_executor=pipeline_executor,
        prompt_config_manager=pcm,
    )
    cr = ConfigResolver(profiles_dir=profiles_dir, presets_dir=presets_dir)
    tm.set_config_resolver(cr)

    app = create_app(tm, cr, prompt_config_manager=pcm)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port,
                log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
