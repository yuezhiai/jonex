# -*- coding:utf-8 -*-
"""「更新模型」：文档行操作 → Ontology Builder 再解析 → 草稿 → 人工确认写回 KB schema。

流程（与 Ontology Builder 的 `apps/server/src/kbRefresh` 同契约）：

1. `start`：导出当前 KB compiled schema YAML + 选中文档的已解析切片正文，
   POST `{ONTOLOGY_BUILDER_BASE_URL}/api/kb-refresh/runs`（异步任务，202 + jobId）。
2. `get_status`：轮询 Ontology Builder 任务；远端 done 后**先在本地 dry-run 校验**草稿，
   只有无 errors 才置 `ready`，否则 `invalid`（草稿照样回传，供人看错误）。
3. `apply`：人审确认后，用当前 `schema_version` 做乐观锁，`dry_run=False` 导入。

任务表在进程内存里（KB 服务单实例）；服务重启丢任务只会让前端重新发起，
不写任何中间态到数据库。
"""
from __future__ import annotations

import datetime
import os
import uuid
from typing import Any, Optional, Protocol

import httpx

from jonex_core.common.exceptions import (
    JonexException,
    ResourceConflictError,
    ResourceNotFoundError,
    ServiceUnavailableError,
    UpstreamServiceError,
)
from jonex_core.common.tenant import require_tenant

DEFAULT_ONTOLOGY_BUILDER_BASE_URL = "http://host.docker.internal:3000"
MODEL_UPDATE_MAX_INPUT_CHARS = 900_000
MODEL_UPDATE_HTTP_TIMEOUT = 30.0
MODEL_UPDATE_MAX_JOBS = 200

_TERMINAL = frozenset({"ready", "invalid", "failed", "applied"})


class JsonTransport(Protocol):
    async def post_json(self, url: str, payload: dict) -> dict: ...
    async def get_json(self, url: str) -> dict: ...


class HttpxTransport:
    """最小 JSON 传输；测试注入假实现，生产走 httpx。"""

    def __init__(self, timeout: float = MODEL_UPDATE_HTTP_TIMEOUT):
        self._timeout = timeout

    async def post_json(self, url: str, payload: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(
                message=f"Ontology Builder 服务不可达: {exc}",
            ) from exc
        return self._decode(url, resp)

    async def get_json(self, url: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(
                message=f"Ontology Builder 服务不可达: {exc}",
            ) from exc
        return self._decode(url, resp)

    @staticmethod
    def _decode(url: str, resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            detail = resp.text[:500]
            raise UpstreamServiceError(
                message=f"Ontology Builder 返回 {resp.status_code}: {detail}",
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise UpstreamServiceError(message="Ontology Builder 返回了非 JSON 响应") from exc
        if not isinstance(data, dict):
            raise UpstreamServiceError(message="Ontology Builder 返回了意外形状的响应")
        return data


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class ModelUpdateService:
    def __init__(
        self,
        *,
        compiler: Any = None,
        documents: Any = None,
        knowledge_infos: Any = None,
        transport: Optional[JsonTransport] = None,
        base_url: Optional[str] = None,
        max_input_chars: int = MODEL_UPDATE_MAX_INPUT_CHARS,
    ) -> None:
        self.compiler = compiler
        self.documents = documents
        self.knowledge_infos = knowledge_infos
        self.transport = transport or HttpxTransport()
        # 新名优先；`NEXONTO_BASE_URL` 仅作存量部署的兼容回落。
        self.base_url = (
            base_url
            or os.getenv("ONTOLOGY_BUILDER_BASE_URL")
            or os.getenv("NEXONTO_BASE_URL")
            or DEFAULT_ONTOLOGY_BUILDER_BASE_URL
        ).rstrip("/")
        self.max_input_chars = max_input_chars
        self._jobs: dict[str, dict] = {}

    # ── public actions ──────────────────────────────────────────────

    async def start(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        document_id: str,
        *,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> dict:
        """收集输入并提交 Ontology Builder 异步任务。依赖不可达直接失败，不建空任务。"""
        tenant_id = require_tenant(tenant_id)

        schema = await self.compiler.export_compiled_schema_yaml(
            knowledge_base_id, tenant_id,
        )
        schema_yaml = schema.get("yaml_text") or ""
        if not schema_yaml.strip():
            raise ResourceNotFoundError(
                message="当前知识库还没有可导出的 compiled schema，请先编译或初始化 schema",
            )

        document = await self.documents.get_document(
            tenant_id, document_id, {"knowledge_base_id": knowledge_base_id},
        )
        chunk_result = await self.documents.get_document_chunks(tenant_id, document_id)
        text, warnings = self._render_document_text(chunk_result.get("chunks") or [])
        for warning in schema.get("warnings") or []:
            warnings.append(str(warning))
        if not text.strip():
            raise ResourceNotFoundError(
                message="该文档还没有已解析的切片内容，请等待解析完成后再更新模型",
            )

        kb_name = ""
        try:
            kb = await self.knowledge_infos.get(knowledge_base_id, tenant_id)
            kb_name = str(kb.get("name") or "")
        except JonexException:
            # KB 名称只是给 Ontology Builder 的展示标签，取不到不影响主流程。
            kb_name = ""

        file_name = (
            document.get("file_name")
            or document.get("title")
            or f"{document_id}.md"
        )
        payload = {
            "kbId": knowledge_base_id,
            "kbName": kb_name or knowledge_base_id,
            "schemaYaml": schema_yaml,
            "files": [{"name": file_name, "path": file_name, "text": text}],
        }
        try:
            remote = await self.transport.post_json(
                f"{self.base_url}/api/kb-refresh/runs", payload,
            )
        except JonexException as exc:
            raise UpstreamServiceError(
                message=f"提交模型更新任务失败: {exc.message}",
            ) from exc

        remote_job_id = str(remote.get("jobId") or "")
        if not remote_job_id:
            raise UpstreamServiceError(message="Ontology Builder 未返回 jobId")

        job_id = uuid.uuid4().hex
        now = _now()
        self._prune()
        self._jobs[job_id] = {
            "job_id": job_id,
            "tenant_id": tenant_id,
            "knowledge_base_id": knowledge_base_id,
            "document_id": document_id,
            "remote_job_id": remote_job_id,
            "status": "running",
            "stage": "queued",
            "schema_yaml": None,
            "stats": None,
            "issues": [],
            "draft": None,
            "warnings": warnings,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        return self._public(self._jobs[job_id])

    async def get_status(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        job_id: str,
    ) -> dict:
        job = self._get_job(tenant_id, knowledge_base_id, job_id)
        if job["status"] in _TERMINAL:
            return self._public(job)

        remote = await self.transport.get_json(
            f"{self.base_url}/api/kb-refresh/runs/{job['remote_job_id']}",
        )
        remote_status = str(remote.get("status") or "")
        job["stage"] = remote_status or job["stage"]
        job["updated_at"] = _now()
        # 归一化阶段剔除的不合规条目（如 value_range 落在 string 字段）由 Ontology
        # Builder 回带；原样并进 warnings，让用户在任务详情里看到「丢了什么、为什么」。
        remote_warnings = [
            str(w).strip() for w in (remote.get("warnings") or []) if str(w).strip()
        ]
        if remote_warnings:
            existing = list(job.get("warnings") or [])
            job["warnings"] = [*existing, *[w for w in remote_warnings if w not in existing]]

        if remote_status == "failed":
            job["status"] = "failed"
            job["error"] = str(remote.get("error") or "Ontology Builder 任务失败")
            return self._public(job)

        if remote_status != "done":
            return self._public(job)

        yaml_text = str(remote.get("schemaYaml") or "")
        if not yaml_text.strip():
            job["status"] = "failed"
            job["error"] = "Ontology Builder 任务完成但没有返回 schema 草稿"
            return self._public(job)

        job["schema_yaml"] = yaml_text
        job["issues"] = list(remote.get("issues") or [])
        job["stats"] = remote.get("stats")
        validation, abstract_active = await self._dry_run(job)
        errors = list(validation.get("errors") or [])
        if abstract_active:
            # 防洗行护栏：有 active 抽象 schema 时 KB 级 compiled schema 不可直接保存。
            # 提前给出可执行的提示，而不是等人点「确认导入」再吃一句 1003。
            errors.append(
                "该知识库已启用抽象 schema（概念对齐层），知识库级 schema 不可直接写入；"
                "请先在抽象 schema 编辑器中修改或停用后再导入",
            )
        job["draft"] = {
            "yaml_text": yaml_text,
            "summary": validation.get("summary"),
            "warnings": list(validation.get("warnings") or []),
            "errors": errors,
        }
        job["status"] = "invalid" if errors else "ready"
        return self._public(job)

    async def apply(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        job_id: str,
        *,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> dict:
        job = self._get_job(tenant_id, knowledge_base_id, job_id)
        if job["status"] == "applied":
            raise ResourceConflictError(message="该草稿已导入，请重新发起模型更新")
        if job["status"] != "ready":
            raise ResourceConflictError(
                message="草稿尚未就绪或校验未通过，不能导入",
            )

        schema = await self.compiler.get_compiled_schema(
            tenant_id, knowledge_base_id, auto_compile=False,
        )
        expected_version = int((schema or {}).get("schema_version") or 1)
        result = await self.compiler.import_compiled_schema_yaml(
            knowledge_base_id,
            tenant_id,
            job["schema_yaml"],
            expected_version,
            dry_run=False,
        )
        errors = list(result.get("errors") or [])
        if errors:
            job["status"] = "invalid"
            job["draft"] = {
                "yaml_text": job["schema_yaml"],
                "summary": result.get("summary"),
                "warnings": list(result.get("warnings") or []),
                "errors": errors,
            }
            raise ResourceConflictError(message="草稿校验失败：" + "; ".join(errors))

        job["status"] = "applied"
        job["updated_at"] = _now()
        return {
            "job_id": job["job_id"],
            "status": "applied",
            "schema_version": result.get("schema_version"),
            "summary": result.get("summary"),
            "warnings": list(result.get("warnings") or []),
        }

    # ── internals ──────────────────────────────────────────────────

    async def _dry_run(self, job: dict) -> tuple[dict, bool]:
        """本地 dry-run 校验草稿；另回报「KB 级 schema 是否被抽象投影接管」。"""
        schema = await self.compiler.get_compiled_schema(
            job["tenant_id"], job["knowledge_base_id"], auto_compile=False,
        )
        expected_version = int((schema or {}).get("schema_version") or 1)
        result = await self.compiler.import_compiled_schema_yaml(
            job["knowledge_base_id"],
            job["tenant_id"],
            job["schema_yaml"],
            expected_version,
            dry_run=True,
        )
        return result, (schema or {}).get("source_type") == "abstract"

    def _render_document_text(self, chunks: list[dict]) -> tuple[str, list[str]]:
        ordered = sorted(
            chunks,
            key=lambda c: (
                c.get("chunk_order_index") is None,
                c.get("chunk_order_index") or 0,
            ),
        )
        parts = [
            str(c.get("content") or "").strip()
            for c in ordered
            if str(c.get("content") or "").strip()
        ]
        text = "\n\n".join(parts)
        warnings: list[str] = []
        if len(text) > self.max_input_chars:
            text = text[: self.max_input_chars]
            warnings.append(
                f"文档正文过长，已截断至 {self.max_input_chars} 字符后再解析",
            )
        return text, warnings

    def _get_job(self, tenant_id: str, knowledge_base_id: str, job_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        job = self._jobs.get(job_id)
        if (
            job is None
            or job["tenant_id"] != tenant_id
            or job["knowledge_base_id"] != knowledge_base_id
        ):
            raise ResourceNotFoundError(message="模型更新任务不存在或已过期")
        return job

    def _prune(self) -> None:
        if len(self._jobs) < MODEL_UPDATE_MAX_JOBS:
            return
        ordered = sorted(self._jobs.values(), key=lambda j: j["created_at"])
        for job in ordered[: len(self._jobs) - MODEL_UPDATE_MAX_JOBS + 1]:
            self._jobs.pop(job["job_id"], None)

    @staticmethod
    def _public(job: dict) -> dict:
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "stage": job["stage"],
            "document_id": job["document_id"],
            "stats": job["stats"],
            "issues": job["issues"],
            "draft": job["draft"],
            "warnings": job["warnings"],
            "error": job["error"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
        }
