"""Document application service for Knowledge Base."""

import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import and_, delete, or_, select

from jonex_core.capability.atomic.rag.client import get_rag_client
from jonex_core.common.audit import schedule_emit
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.database import get_db_session
from jonex_core.common.file_source_util import parse_file_source
from jonex_core.common.exceptions import (
    InvalidParameterError,
    PermissionDeniedError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.neo4j_client import get_neo4j_driver
from jonex_core.common.object_storage import (
    build_asset_key,  # [jonex] §image-refs P2-2: 图片资产对象键
    build_object_key,
    get_object_storage,
    get_object_storage_for,
)
from jonex_core.common.tenant import require_tenant

from ..models import DocStatus, DocumentTag, KnowledgeDocument, OntologyStatus
from ..models.data_source import KnowledgeDataSource
from ..repository import (
    FolderRepository,
    KnowledgeDocumentRepository,
    OntologyGraphRepository,
    UNCLASSIFIED_SENTINEL,
)
from ..dtos import (
    BatchMoveDocumentsRequest,
    DocumentListRequest,
    DocumentScopeRequest,
    DocumentUploadRequest,
    SetDocumentFolderRequest,
)

logger = logging.getLogger(__name__)


# [jonex] 线性状态（phase）→ SQLAlchemy 谓词。单一事实来源，供 list_documents 与
# documents_stats 复用。与前端 deriveDocPhase / 设计文档 §4.1 表一致。
_PHASE_PREDICATE = {
    "pending_parse":   lambda: KnowledgeDocument.status == DocStatus.PENDING.value,
    "parsing":         lambda: KnowledgeDocument.status == DocStatus.PARSING.value,
    "ingesting":       lambda: KnowledgeDocument.status == DocStatus.INGESTING.value,
    "parse_failed":    lambda: KnowledgeDocument.status == DocStatus.FAILED.value,
    "pending_compile": lambda: and_(
        KnowledgeDocument.status == DocStatus.READY.value,
        KnowledgeDocument.ontology_status == OntologyStatus.PENDING.value,
    ),
    "compiling": lambda: and_(
        KnowledgeDocument.status == DocStatus.READY.value,
        KnowledgeDocument.ontology_status == OntologyStatus.EXTRACTING.value,
    ),
    "compiled": lambda: and_(
        KnowledgeDocument.status == DocStatus.READY.value,
        KnowledgeDocument.ontology_status == OntologyStatus.READY.value,
    ),
    "compile_failed": lambda: and_(
        KnowledgeDocument.status == DocStatus.READY.value,
        KnowledgeDocument.ontology_status == OntologyStatus.FAILED.value,
    ),
}

# [jonex] openkb 文档的编译状态谓词：openkb 不抽本体（ontology_status 恒 READY，
# reconciliation 强制），编译状态唯一事实来源是 llm_wiki_compile_status
# （NULL=未编译 / stale=已过期待重编 / compiling / compiled / failed）。
# 解析类 phase（pending_parse/parsing/ingesting/parse_failed）与 lightrag 一致（status 列语义相同），
# 仅覆盖编译 4 个 phase。供 list_documents / documents_stats 按 kb_type 分流使用。
_OPENKB_PHASE_PREDICATE = {
    **_PHASE_PREDICATE,
    "pending_compile": lambda: and_(
        KnowledgeDocument.status == DocStatus.READY.value,
        or_(
            KnowledgeDocument.llm_wiki_compile_status.is_(None),
            KnowledgeDocument.llm_wiki_compile_status == "stale",
        ),
    ),
    "compiling": lambda: and_(
        KnowledgeDocument.status == DocStatus.READY.value,
        KnowledgeDocument.llm_wiki_compile_status == "compiling",
    ),
    "compiled": lambda: and_(
        KnowledgeDocument.status == DocStatus.READY.value,
        KnowledgeDocument.llm_wiki_compile_status == "compiled",
    ),
    "compile_failed": lambda: and_(
        KnowledgeDocument.status == DocStatus.READY.value,
        KnowledgeDocument.llm_wiki_compile_status == "failed",
    ),
}


def _phase_condition(phases: Optional[list[str]], predicates: Optional[dict] = None):
    """多值 phase → OR-of-AND 谓词；无有效 phase 返回 None。

    predicates 默认 lightrag 语义（_PHASE_PREDICATE）；openkb KB 由调用方传入
    _OPENKB_PHASE_PREDICATE（编译 phase 按 llm_wiki_compile_status 过滤）。
    """
    if not phases:
        return None
    preds = predicates if predicates is not None else _PHASE_PREDICATE
    conds = [preds[p]() for p in phases if p in preds]
    if not conds:
        return None
    return or_(*conds) if len(conds) > 1 else conds[0]


def _file_ext(file_name: str) -> str:
    """取文件名的小写扩展名（不含点），无扩展名返回空串。例：'a.MP4' → 'mp4'。"""
    return os.path.splitext(file_name or "")[1].lower().lstrip(".")


def _normalize_parser_exts(file_types: Any) -> set[str]:
    """把 parser_configs.file_types 归一为小写扩展名集合（不含点）。

    值可能是 list（JSONB 已反序列化）或 JSON 字符串；元素如 'MP4' / '.mp4' → 'mp4'。
    """
    raw = file_types
    if raw is None:
        return set()
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return set()
    if not isinstance(raw, list):
        return set()
    return {str(v).strip().lower().lstrip(".") for v in raw if str(v).strip()}


def _payload(model_or_dict: Any) -> dict[str, Any]:
    if isinstance(model_or_dict, dict):
        return model_or_dict
    if hasattr(model_or_dict, "model_dump"):
        return model_or_dict.model_dump(exclude_none=True)
    return model_or_dict.dict(exclude_none=True)


def _audit_user_id(user_id: Optional[str]) -> Optional[int]:
    """安全的将 invoke 链路中的字符串 user_id 转为 int"""
    if user_id and user_id.isdigit():
        return int(user_id)
    return None


# ── [jonex] §table-grid-v2 O7: 新旧格式 chunk 指纹 ──
# （docs/table-parsing-retrieval-governance-plan.md §11.3：表格格式一变，
#   content_doc_id 全变；reparse_strict 删除失败时新旧格式会在向量库共存，
#   检索结果混入旧格式碎片且很难察觉。）

_STALE_COL_RE = re.compile(r"col_\d+")
# C2 的硬切残片形态多样：完整 <table>、以及被 token 硬切出的 <td/<th 行片
# （如「>\n</tr><tr>\n<td>泰国</td>」）。只认开标签，</tr> 等闭合标签在代码
# 示例里太常见，不纳入。
_STALE_RAW_TABLE_RE = re.compile(r"<table|<td|<th", re.IGNORECASE)


def _is_stale_table_chunk(content: str, file_path: str) -> tuple[bool, str]:
    """O7 格式指纹：判定一个 chunk 是否属「旧格式表格」残留。

    新格式表格 chunk 必带 ``table_sig=``（file_source 旁路，§20 写入）——
    即使 L2 表头推断兜底产出了 ``col_N`` 列名，也不判旧格式。
    旧格式指纹：file_source 无 ``table_sig`` 且（正文含 ``col_`` 占位 或
    裸 ``<table`` 标签残片）。

    豁免（本地 reparse 实测校准，2026-08-14）：
    - ``ctype=table_summary``：摘要文本描述「列名清单：col_0、col_1…」会含
      col_N 字样，属正常文字而非占位残留；
    - 正文含 ``data-jonex-native`` 标记：xlsx 主轨产出的新格式 fallback
      HTML（单行表归一化无数据 → 裸 HTML 入库，是既定行为）。

    Returns:
        ``(is_stale, reason)``，reason ∈ ``""`` / ``"col_placeholder"`` /
        ``"raw_html_table"``。
    """
    parsed = parse_file_source(file_path) if file_path else {}
    if parsed.get("table_sig"):
        return False, ""
    if parsed.get("chunk_type") == "table_summary":
        return False, ""
    content = content or ""
    if "data-jonex-native" in content:
        return False, ""
    if _STALE_COL_RE.search(content):
        return True, "col_placeholder"
    if _STALE_RAW_TABLE_RE.search(content):
        return True, "raw_html_table"
    return False, ""


class DocumentService:
    """Tenant-scoped document metadata and RAG ingestion orchestration."""

    # 手动上传归属的数据源接入类型
    _FILE_ACCESS_TYPE = "file"

    async def _require_file_data_source_id(self, tenant_id: str, kb_id: str) -> str:
        """返回该 KB 的「文件上传」(file) 数据源 id；不存在则报错。

        手动上传要求知识库已配置 file 数据源（通过数据源管理创建）。上传服务只做
        归属，不在上传时隐式创建数据源——能调用上传即意味着该数据源应已存在。
        """
        async with get_db_session() as session:
            ds = (
                await session.execute(
                    select(KnowledgeDataSource)
                    .where(
                        KnowledgeDataSource.tenant_id == tenant_id,
                        KnowledgeDataSource.knowledge_base_id == kb_id,
                        KnowledgeDataSource.access_type == self._FILE_ACCESS_TYPE,
                        KnowledgeDataSource.is_deleted == 0,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        if ds is None:
            raise ResourceNotFoundError(
                message=translate("err.kb.no_file_datasource", fallback="该知识库未配置「文件上传」数据源，请先在数据源设置中添加后再上传")  ,  # 原消息
                details={"knowledge_base_id": kb_id, "access_type": self._FILE_ACCESS_TYPE},
            )
        return ds.id

    @staticmethod
    async def _resolve_parser_preset(tenant_id: str, kb_id: str, file_name: str) -> tuple[str, Optional[str]]:
        """按文件后缀在该 KB 已配置的解析器中定位，返回 active 的 parser_config_id
        （= raganything preset name）。

        解析逻辑：文件后缀 → 命中"某个已选中解析器声明的 file_types"。KB 每个
        parser_type 类目选一个解析器（knowledge_parser_settings），解析器的 file_types
        （business_domain.parser_configs）是"后缀 → 解析器"的唯一事实来源，无需任何
        扩展名词表映射。

        严格模式：定位不到有效解析器 → 抛 InvalidParameterError。调用方据此把文档标记为
        解析失败、保留记录、不推送到 atomic-rag。

        触发报错的情形：
          - 文件无扩展名；
          - 该 KB 没有任何 active 解析设置绑定的 active 解析器的 file_types 覆盖该后缀。
        """
        from sqlalchemy import text as _sql_text

        ext = _file_ext(file_name)
        if not ext:
            raise InvalidParameterError(
                message=translate("err.doc.no_extension", params={"file_name": file_name}, fallback=f"文件缺少扩展名，无法定位解析器：{file_name}")  ,  # 原消息
                details={"file_name": file_name, "knowledge_base_id": kb_id},
            )

        async with get_db_session() as session:
            # 该 KB 的 active 解析设置 join 其选中的 active 解析器，取出 file_types
            rows = (
                await session.execute(
                    _sql_text(
                        "SELECT pc.id AS parser_config_id, pc.file_types AS file_types, "
                        "       ps.prompt_config_id AS prompt_config_id "
                        "FROM knowledge_base.knowledge_parser_settings ps "
                        "JOIN business_domain.parser_configs pc "
                        "  ON pc.id = ps.parser_config_id "
                        " AND pc.is_deleted = 0 "
                        " AND pc.status = 'active' "
                        "WHERE ps.tenant_id = :tid "
                        "  AND ps.knowledge_base_id = :kb "
                        "  AND ps.is_deleted = 0 "
                        "  AND ps.status = 'active' "
                        "  AND ps.parser_config_id IS NOT NULL"
                    ),
                    {"tid": tenant_id, "kb": kb_id},
                )
            ).fetchall()

        for row in rows:
            if ext in _normalize_parser_exts(row.file_types):
                # 返回 (parser_config_id, prompt_config_id)；后者用于解析时下发 prompt_ids
                return row.parser_config_id, row.prompt_config_id

        raise InvalidParameterError(
            message=translate("err.kb.no_parser_for_ext", params={"ext": ext}, fallback=f"该知识库未配置支持「.{ext}」文件的解析器，请先在解析设置中配置后再上传")  ,  # 原消息
            details={"knowledge_base_id": kb_id, "file_ext": ext, "file_name": file_name},
        )

    @staticmethod
    async def _compute_source_hash(storage_backend: str, storage_key: str) -> str:
        """[jonex] R1-c：计算源文件内容 md5，用于 reparse 跳过未变化文档。

        下载完整文件字节并计算 md5 哈希。hash 取不到（临时错误）→ 调用方应 fail-open
        继续走 reparse，不因 hash 计算失败阻塞正常业务。
        """
        storage = get_object_storage()
        try:
            data = await storage.get_bytes(storage_key)
        except Exception as exc:
            logger = logging.getLogger(__name__)
            logger.warning(
                "R1-c get_bytes failed for key=%s backend=%s: %s",
                storage_key, storage_backend, exc,
            )
            return ""
        return hashlib.md5(data).hexdigest()

    @staticmethod
    async def _find_duplicate(tenant_id: str, kb_id: str, content_hash: str) -> Optional[Any]:
        """[jonex] 上传去重：同 KB 内返回内容 hash 相同的活跃文档（未删除且非 failed）。

        返回 row（含 id / file_name）或 None。failed / 已删除的文档不算重复，
        允许解析失败后重新上传重试。
        """
        from sqlalchemy import text as _sql_text

        async with get_db_session() as session:
            row = (
                await session.execute(
                    _sql_text(
                        "SELECT id, file_name FROM knowledge_base.knowledge_documents "
                        "WHERE tenant_id = :tid AND knowledge_base_id = :kb "
                        "  AND content_hash = :h "
                        "  AND is_deleted = 0 AND status <> 'failed' "
                        "LIMIT 1"
                    ),
                    {"tid": tenant_id, "kb": kb_id, "h": content_hash},
                )
            ).fetchone()
        return row

    @staticmethod
    async def _compute_config_fingerprint(
        tenant_id: str, kb_id: str, file_name: str,
    ) -> tuple[str, str, Optional[str], int]:
        """[jonex] R1-c：计算解析配置指纹，用于 reparse 检测配置变更。

        指纹包含 preset + parser updated_at + prompt id + prompt updated_at
        + compiled schema version。任意一项变化 → 指纹不同 → 自动放行重解析。

        返回 (fingerprint_hash, preset_name, prompt_config_id, schema_version)。
        后三项可缓存复用，避免后续 RAG 调用阶段重复解析。
        """
        from sqlalchemy import text as _sql_text

        preset, prompt_config_id = await DocumentService._resolve_parser_preset(
            tenant_id, kb_id, file_name,
        )

        # 获取 parser_config 和 prompt 的 updated_at（一次 DB 往返）
        parser_ts = ""
        prompt_ts = ""
        async with get_db_session() as session:
            if preset:
                row = (await session.execute(
                    _sql_text(
                        "SELECT updated_at FROM business_domain.parser_configs "
                        "WHERE id = :id"
                    ),
                    {"id": preset},
                )).fetchone()
                if row and row.updated_at:
                    parser_ts = row.updated_at.isoformat()
            if prompt_config_id:
                row = (await session.execute(
                    _sql_text(
                        "SELECT updated_at FROM business_domain.prompt_templates "
                        "WHERE id = :id AND (tenant_id = :tid OR tenant_id IS NULL)"
                    ),
                    {"id": prompt_config_id, "tid": tenant_id},
                )).fetchone()
                if row and row.updated_at:
                    prompt_ts = row.updated_at.isoformat()

        # schema_version：与主流程一致使用 auto_compile=True，避免首次编译导致
        # 指纹版本（0）≠ 主流程版本（1）而"多跑一次"。
        schema_ver = 0
        try:
            from .ontology_compiler import OntologyCompiler  # noqa: F811
            schema = await OntologyCompiler().get_compiled_schema(
                tenant_id, kb_id, auto_compile=True,
            )
            if schema:
                schema_ver = int(schema.get("schema_version", 0) or 0)
        except Exception:
            pass

        fingerprint = (
            f"{preset}|{parser_ts}|{prompt_config_id or ''}|{prompt_ts}|{schema_ver}"
        )
        return (
            hashlib.md5(fingerprint.encode()).hexdigest(),
            preset, prompt_config_id, schema_ver,
        )

    async def upload_document(self, tenant_id: str, request: DocumentUploadRequest | dict, *, user_id: Optional[str] = None, username: Optional[str] = None, ip: Optional[str] = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        data = _payload(request)
        req = DocumentUploadRequest(**data)
        metadata = dict(req.metadata or {})

        # 统一来源标记：未显式归属（即手动上传，同步/推送路径已自带 data_source_id）时，
        # 归属到该 KB 已存在的 file 数据源；不存在则报错，不隐式创建。
        if not metadata.get("data_source_id"):
            metadata["data_source_id"] = await self._require_file_data_source_id(tenant_id, req.knowledge_base_id)
            metadata.setdefault("source", self._FILE_ACCESS_TYPE)
        # 冗余真实列：文档来源方式（file / api / storage / api_push），文档数统计按此列分组
        data_source_type = metadata.get("source")

        # [jonex] R2-a0: 同事务写入提交锚点 —— 宽限期计时从这里开始
        # [jonex] P1-1: 统一用 datetime.now()（naive 本地时间），与 DB TimestampMixin / patrol 口径一致
        metadata["submit_started_at"] = datetime.now().isoformat()
        # [jonex] P0-3: patrol 计时基准 —— 进入 PARSING 时打点，
        # 解耦 TimestampMixin.updated_at onupdate 心跳污染（_death_candidate 每 30s
        # 写一次 DB → onupdate 刷新 updated_at → patrol elapsed 永远到不了 SOFT）。
        metadata["parsing_started_at"] = datetime.now().isoformat()

        storage_key = req.storage_key or req.file_path
        storage_backend = req.storage_backend
        # 未显式指定存储后端时，从环境变量自动推断
        if storage_backend == "local" and os.getenv("OBJECT_STORAGE_BACKEND", "local") == "cos":
            storage_backend = "cos"

        # file_path 由存储后端统一推导（下游 atomic-rag 解析用）：
        #  - 对象存储后端（cos 等）：通过 storage_key 下载，file_path 仅作标识 → 用 storage_key；
        #  - local 后端：解析需可直接读取的绝对路径，由对象存储后端把 key 解析为共享卷绝对路径。
        if storage_backend == "cos":
            file_path = req.file_path or storage_key
        else:
            file_path = get_object_storage().fs_path(storage_key) or req.file_path or storage_key

        # [jonex] 上传去重：同 KB 内内容 md5 相同的活跃文档已存在则拒绝，避免重复入库。
        # （fail-open：hash 取不到 → 放行，不因临时存储错误阻塞上传）
        content_hash = await self._compute_source_hash(storage_backend, storage_key)
        if content_hash:
            dup = await self._find_duplicate(tenant_id, req.knowledge_base_id, content_hash)
            if dup is not None:
                raise ResourceConflictError(
                    message=translate(
                        "err.doc.duplicate",
                        params={"file_name": dup.file_name},
                        fallback=f"知识库中已存在相同内容的文档「{dup.file_name}」，请勿重复上传",
                    ),
                    details={
                        "knowledge_base_id": req.knowledge_base_id,
                        "duplicate_document_id": dup.id,
                    },
                )

        doc_id = req.doc_id or None  # 预生成 doc_id（COS 直传模式），None 则自动 UUID
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.create(
                KnowledgeDocument(
                    id=doc_id,
                    tenant_id=tenant_id,
                    file_name=req.file_name,
                    file_path=file_path,
                    file_size=req.file_size,
                    mime_type=req.mime_type,
                    knowledge_base_id=req.knowledge_base_id,
                    storage_backend=storage_backend,
                    storage_key=storage_key,
                    content_hash=content_hash or None,  # [jonex] 上传去重持久化
                    status=DocStatus.PARSING.value,
                    ontology_status=OntologyStatus.PENDING.value,
                    folder_id=req.folder_id,
                    extra_metadata=metadata,
                    data_source_type=data_source_type,
                )
            )
            doc_id = doc.id
            doc_dict = doc.to_dict()

        uid = _audit_user_id(user_id)
        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": uid,
            "username": username,
            "ip": ip,
            "log_type": "OPERATION",
            "action": "document.upload",
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(doc_id),
            "request_params": {"file_name": req.file_name, "knowledge_base_id": req.knowledge_base_id},
        })
        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": uid,
            "username": username,
            "ip": ip,
            "log_type": "TASK",
            "action": "document.parse",
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(doc_id),
        })

        # COS 后端：确认对象已存在后再入队解析，避免竞态（D9）
        if storage_backend == "cos":
            exists = await get_object_storage().head_object(storage_key)
            if not exists:
                raise ResourceNotFoundError(
                    message=translate("err.cos.object_not_found", params={"storage_key": storage_key}, fallback=f"COS 对象不存在或上传未完成: {storage_key}")  ,  # 原消息
                    details={"storage_key": storage_key},
                )

        # Ensure compiled schema exists for this KB (non-blocking)
        schema = None
        schema_version = 0
        try:
            from .ontology_compiler import OntologyCompiler
            compiler = OntologyCompiler()
            schema = await compiler.get_compiled_schema(tenant_id, req.knowledge_base_id, auto_compile=True)
            if schema is None:
                logger.warning("No compiled schema available for KB %s after auto-compile", req.knowledge_base_id)
            else:
                schema_version = int(schema.get("schema_version", 0) or 0)
        except Exception as exc:
            logger.warning("Failed to ensure compiled schema for KB %s: %s", req.knowledge_base_id, exc)

        try:
            # 按文件类型解析该 KB 配置的解析器 preset。解析不到（类型不支持 / 未配置 /
            # 解析器非 active）会抛 InvalidParameterError，落入下方 except：文档标记为
            # 解析失败、保留记录、不推送到 atomic-rag。
            preset, prompt_config_id = await self._resolve_parser_preset(
                tenant_id, req.knowledge_base_id, req.file_name
            )
            # [jonex] 主解析提示词下发：该类目关联了 prompt 配置则带上 prompt_ids
            prompt_ids = [prompt_config_id] if prompt_config_id else []

            # ── [jonex] kb_type 分流 ───
            kb_type = await self._get_kb_type(tenant_id, req.knowledge_base_id)
            # [jonex] OpenKB KB 级互斥：openkb 文档只解析产出 markdown，不写 LightRAG/Neo4j
            execution_mode = "parse_only" if kb_type == "openkb" else "full"

            # [jonex] R1：生成确定性幂等键，insert 响应丢失后重试拿回同一 task_id
            # [jonex] P2: 与对账侧 _build_idempotency_key 同口径，读 content_generation 而不硬编码 :0
            gen = getattr(doc, "content_generation", 0) or 0
            idempotency_key = f"insert:{tenant_id}:{req.knowledge_base_id}:{doc_id}:{gen}"
            rag_result = await get_rag_client().insert(
                file_path=file_path,
                tenant_id=tenant_id,
                knowledge_base_id=req.knowledge_base_id,
                document_id=doc_id,
                ontology_schema=schema,           # [jonex] push compiled schema 到抽取链路
                storage_backend=storage_backend,  # P3: COS 本地后端
                storage_key=storage_key,
                preset=preset,                    # KB 按文件类型选择的解析器（v2 preset 链路）
                prompt_ids=prompt_ids,            # KB 主解析提示词
                schema_version=schema_version,    # [jonex] P1-E：供对账写图前 fencing
                execution_mode=execution_mode,    # [jonex] OpenKB parse_only 分流
                idempotency_key=idempotency_key,  # [jonex] R1
            )
        except Exception as exc:
            logger.exception("Knowledge document ingestion failed: %s", doc_id)
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                doc = await repo.get_required(doc_id, tenant_id)
                await repo.set_status(doc, DocStatus.FAILED, error_message=str(exc))
                # [jonex] R2-a0: 清除提交锚点（已判 FAILED，不可残留掩盖后续故障）
                # [jonex] N1: SQLAlchemy JSON 列不追踪原地 pop，必须整体重赋值
                meta = dict(doc.extra_metadata or {})
                meta.pop("submit_started_at", None)
                doc.extra_metadata = meta
                doc_dict = doc.to_dict()
            schedule_emit({
                "tenant_id": tenant_id,
                "user_id": uid,
                "username": username,
                "ip": ip,
                "log_type": "TASK",
                "action": "document.parse_failed",
                "outcome": "FAILED",
                "service_name": "knowledge_base",
                "resource": ResourceType.DOCUMENT.value,
                "resource_id": str(doc_id),
                "error_message": str(exc)[:1000],
            })
            return doc_dict

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(doc_id, tenant_id)
            status = DocStatus.READY if not rag_result.get("task_id") else DocStatus.PARSING
            await repo.set_status(
                doc,
                status,
                rag_task_id=rag_result.get("task_id"),
                rag_doc_ids=rag_result.get("doc_ids") or rag_result.get("document_ids") or [],
            )
            # [jonex] openkb KB 不抽本体：上传时直接标 ontology_status=READY，
            # 避免对账巡检前 30s 窗口内 ontology_status=PENDING（DB 默认值），
            # 导致前端显示误导的「知识抽取入库中」。
            if kb_type == "openkb":
                await repo.set_ontology_status(doc, OntologyStatus.READY)
            # [jonex] P1-E：记录本文档应归类到的目标 schema 版本
            if schema_version:
                doc.ontology_target_schema_version = schema_version
            doc.extra_metadata = {**(doc.extra_metadata or {}), "rag_result": rag_result, "kb_type": kb_type}
            # [jonex] R2-a0: 清除提交锚点（task_id 已写回，不再需要宽限期保护）
            doc.extra_metadata.pop("submit_started_at", None)
            doc_dict = doc.to_dict()

        if status == DocStatus.READY:
            schedule_emit({
                "tenant_id": tenant_id,
                "user_id": uid,
                "username": username,
                "ip": ip,
                "log_type": "TASK",
                "action": "document.parse_done",
                "outcome": "SUCCESS",
                "service_name": "knowledge_base",
                "resource": ResourceType.DOCUMENT.value,
                "resource_id": str(doc_id),
            })

        return doc_dict

    async def generate_upload_url(
        self, tenant_id: str, kb_id: str, file_name: str, content_type: str | None = None,
    ) -> dict:
        """生成 COS 预签名 PUT URL 和 storage_key（D9）。

        前端/网关直传字节到 COS（不经 Sidecar 透传），
        然后再调 upload_document 传 storage_key 确认。
        """
        from uuid import uuid4

        tenant_id = require_tenant(tenant_id)
        doc_id = str(uuid4())
        storage_key = build_object_key(tenant_id, kb_id, doc_id, file_name)

        storage = get_object_storage()
        try:
            upload_url = await storage.presigned_put_url(storage_key, expires=300)
        except Exception:
            # local 后端不支持预签名 PUT 时降级
            upload_url = None

        return {
            "doc_id": doc_id,
            "storage_key": storage_key,
            "upload_url": upload_url,
            "storage_backend": os.getenv("OBJECT_STORAGE_BACKEND", "local"),
        }

    async def get_raw_location(self, tenant_id: str, knowledge_base_id: str = "", document_id: str = "") -> dict:
        """获取文档原文位置信息（校验租户归属后返回）。

        统一 raw 入口：
        - 对象存储后端（cos）：返回 presigned_url，gateway 302 直跳（天然支持 Range/流式）；
        - local 后端：presigned_url 为空，返回 storage_key，gateway 用 FileResponse
          从共享卷流式返回（支持 Range，音视频可拖动/边下边播，不经 Sidecar 传字节）。

        knowledge_base_id 可为空——仅用于可选 KB 级归属校验；租户级鉴权已由 get_required 保证。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
        if knowledge_base_id and doc.knowledge_base_id != knowledge_base_id:
            raise PermissionDeniedError(
                message=f"文档 {document_id} 不属于知识库 {knowledge_base_id}",
                details={"document_id": document_id, "knowledge_base_id": knowledge_base_id,
                         "actual_kb": doc.knowledge_base_id},
            )
        # 按文档自身的 storage_backend 选后端（混合数据时不能用全局 env 单例）
        backend = (doc.storage_backend or "local").strip().lower()
        presigned = ""
        if backend == "cos":
            presigned = await get_object_storage_for("cos").presigned_url(doc.storage_key, tenant_id, expires=300)
        return {
            "storage_backend": backend,
            "storage_key": doc.storage_key,
            "mime_type": doc.mime_type or "application/octet-stream",
            "file_name": doc.file_name,
            "presigned_url": presigned or "",
        }

    async def get_asset_raw_location(self, tenant_id: str, document_id: str,
                                     image_idx: int, ext: str = "",
                                     knowledge_base_id: str = "") -> dict:
        """[jonex] §image-refs P2-2: 获取文档内嵌图片资产的原文位置信息。

        与 get_raw_location 同构的租户归属校验（get_required + 可选 KB 校验）：
        - cos：返回 presigned_url，gateway 302 直跳；
        - local：presigned_url 为空，返回 storage_key，gateway 用 FileResponse
          从共享卷流式返回。

        对象键由 build_asset_key(tenant, doc.kb, document_id, image_idx, ext)
        派生；ext 经白名单归一（非白名单按 png），空 ext 抛 InvalidParameterError
        （不存在该资产的合法键——未上传/上传失败时检索侧本就不会给出 aext）。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
        if knowledge_base_id and doc.knowledge_base_id != knowledge_base_id:
            raise PermissionDeniedError(
                message=f"文档 {document_id} 不属于知识库 {knowledge_base_id}",
                details={"document_id": document_id, "knowledge_base_id": knowledge_base_id,
                         "actual_kb": doc.knowledge_base_id},
            )
        try:
            key = build_asset_key(
                tenant_id, doc.knowledge_base_id, document_id, image_idx, ext,
            )
        except ValueError as exc:
            raise InvalidParameterError(
                message=f"图片资产扩展名无效: {document_id}/img_{image_idx}",
                details={"document_id": document_id, "image_idx": image_idx},
            ) from exc
        # [jonex] review 修正（§image-refs）：资产后端与文档原文后端解耦——
        # 上传（AssetUploadStage）与检索富化（_build_references）都走平台全局
        # 后端 get_object_storage()，raw 端点必须同源。若按 doc.storage_backend
        # 选后端，后端从 local 切 cos 后 reparse 历史文档（storage_backend 仍
        # local）会出现「资产实际在 cos、端点走 local」的错配 404。
        # 全局后端为 local 时 presigned_url 恒为空串，gateway 自动落 FileResponse。
        storage = get_object_storage()
        backend = os.getenv("OBJECT_STORAGE_BACKEND", "local").strip().lower()
        presigned = await storage.presigned_url(key, tenant_id, expires=300) or ""
        safe_ext = key.rsplit(".", 1)[-1].lower()  # 白名单归一后的扩展名
        return {
            "storage_backend": backend,
            "storage_key": key,
            "mime_type": f"image/{safe_ext}",
            "file_name": f"img_{image_idx}.{safe_ext}",
            "presigned_url": presigned or "",
        }

    async def get_raw_url(self, tenant_id: str, knowledge_base_id: str = "", document_id: str = "",
                          user_id: Optional[str] = None, username: Optional[str] = None,
                          ip: Optional[str] = None, mcp_key_id: Optional[str] = None) -> str:
        """获取文档原文的预签名 URL（校验租户归属后签名）。

        用于 GET /documents/{id}/raw 端点（302 重定向）。

        knowledge_base_id 可为空——仅用于可选 KB 级归属校验；租户级鉴权已由 get_required 保证。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
        if knowledge_base_id and doc.knowledge_base_id != knowledge_base_id:
            raise PermissionDeniedError(
                message=f"文档 {document_id} 不属于知识库 {knowledge_base_id}",
                details={"document_id": document_id, "knowledge_base_id": knowledge_base_id,
                         "actual_kb": doc.knowledge_base_id},
            )
        storage = get_object_storage()
        url = await storage.presigned_url(doc.storage_key, tenant_id, expires=300)
        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": user_id or "",
            "username": username or "",
            "ip": ip or "",
            "log_type": "OPERATION",
            "action": "document.download",
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(document_id),
            "request_params": {
                "knowledge_base_id": knowledge_base_id,
                "mcp_key_id": mcp_key_id,
            },
        })
        return url

    async def get_raw_content(self, tenant_id: str, knowledge_base_id: str = "", document_id: str = "",
                              user_id: Optional[str] = None, username: Optional[str] = None,
                              ip: Optional[str] = None, mcp_key_id: Optional[str] = None) -> dict:
        """获取文档原文的字节内容（本地回退，无预签名 URL 时使用）。

        返回 base64 编码的内容 + 元信息，供 gateway 代理文件下载。

        knowledge_base_id 可为空——仅用于可选 KB 级归属校验；租户级鉴权已由 get_required 保证。
        """
        import base64
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
        if knowledge_base_id and doc.knowledge_base_id != knowledge_base_id:
            raise PermissionDeniedError(
                message=f"文档 {document_id} 不属于知识库 {knowledge_base_id}",
                details={"document_id": document_id, "knowledge_base_id": knowledge_base_id,
                         "actual_kb": doc.knowledge_base_id},
            )
        storage = get_object_storage()
        raw = await storage.get_bytes(doc.storage_key)
        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": user_id or "",
            "username": username or "",
            "ip": ip or "",
            "log_type": "OPERATION",
            "action": "document.raw_content",
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(document_id),
            "request_params": {
                "knowledge_base_id": knowledge_base_id,
                "mcp_key_id": mcp_key_id,
            },
        })
        return {
            "content": base64.b64encode(raw).decode("ascii"),
            "mime_type": doc.mime_type or "application/octet-stream",
            "file_name": doc.file_name,
        }

    async def get_document_chunks(self, tenant_id: str, document_id: str) -> dict:
        """按文档 id 查看 chunk 列表（含时间轴/页码等位置元数据）。

        校验租户归属后取文档所属 KB，经 RAGClient 调 atomic-rag v2 的 get_doc_chunks
        （→ LightRAGAdapterV2.get_doc_chunks → action `get_doc_chunks`）。
        doc_id 即 KB knowledge_documents.id，与 file_source 的 doc= 锚点一致。

        对每个 chunk 解析 file_path（file_source 字符串），提取 time_start/time_end
        （视频/音频时间轴）、page_no、char_start/end 等位置元数据，供前端渲染
        时间段标签和定位播放。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)

        # ── [jonex] OpenKB 管线：不入 LightRAG，chunks 从共享卷 parsed markdown 读取 ──
        kb_type = await self._get_kb_type(tenant_id, doc.knowledge_base_id)
        if kb_type == "openkb":
            return self._get_openkb_chunks(document_id)

        result = await get_rag_client().get_doc_chunks(
            document_id=document_id,
            knowledge_base_id=doc.knowledge_base_id,
            tenant_id=tenant_id,
        )
        # 对每个 chunk 解析 file_path，提取位置元数据（time_start/time_end 等）
        chunks = result.get("chunks") or []
        enriched = []
        for c in chunks:
            fp = c.get("file_path", "")
            parsed = parse_file_source(fp) if fp else {}
            enriched.append({
                **c,
                "time_start": parsed.get("time_start"),
                "time_end": parsed.get("time_end"),
                "page_no": parsed.get("page_no"),
                "char_start": parsed.get("char_start"),
                "char_end": parsed.get("char_end"),
                "chunk_index": parsed.get("chunk_index"),
                # [jonex] §table-ctypes / L4.1: 表格元数据旁路透出
                "chunk_type": parsed.get("chunk_type"),
                "table_sig": parsed.get("table_sig"),
                "table_cols": parsed.get("table_cols"),
                "row_start": parsed.get("row_start"),
                "row_end": parsed.get("row_end"),
            })
        return {
            "doc_id": result.get("doc_id", document_id),
            "total": len(enriched),
            "chunks": enriched,
        }

    def _get_openkb_chunks(self, document_id: str) -> dict:
        """[jonex] OpenKB 文档的 chunk 视图：读共享卷 parsed markdown 按块返回。

        OpenKB 管线只解析产出 markdown（parse_only），不入 LightRAG，因此 chunks
        不在 LightRAG。解析产物由 atomic-rag 写到共享卷 inputs/parsed/{doc}/content.md
        （knowledge-base 亦挂载该卷于 KB_INPUT_DIR），这里直接读取并按空行分块返回，
        与 LightRAG chunks 返回结构对齐（content + chunk_index + 位置占位）。

        [jonex] 回归修复（2026-08-14）：8697e5c1（rag-table 治理）误将此方法替换为
        scan_stale_chunks，而 get_document_chunks/get_chunk 的调用未同步——
        openkb 文档 chunks 查询抛 AttributeError。恢复原实现。
        """
        import os
        from pathlib import Path

        inputs_root = os.getenv("KB_INPUT_DIR", "/app/inputs")
        md_path = Path(inputs_root) / "parsed" / document_id / "content.md"
        if not md_path.is_file():
            return {"doc_id": document_id, "total": 0, "chunks": [], "kb_type": "openkb"}

        text = md_path.read_text(encoding="utf-8")
        blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
        chunks = [
            {
                "chunk_id": f"{document_id}#openkb-{i}",
                "content": b,
                # 与 LightRAG chunk 结构对齐：前端解析结果页依赖 content_summary/file_path
                # （getChunkTitle 会读取二者）。OpenKB 无 file_source 锚点，file_path 置空串
                # 而非缺省，避免前端 `.match()` 到 undefined。
                "content_summary": b,
                "file_path": f"doc={document_id}",
                "chunk_index": i,
                "time_start": None,
                "time_end": None,
                "page_no": None,
                "char_start": None,
                "char_end": None,
            }
            for i, b in enumerate(blocks)
        ]
        return {
            "doc_id": document_id,
            "total": len(chunks),
            "chunks": chunks,
            "kb_type": "openkb",
        }

    async def scan_stale_chunks(self, tenant_id: str, document_id: str) -> dict:
        """[jonex] §table-grid-v2 O7: reparse 后置残留扫描。

        按 ``doc=<document_id>`` 扫 LightRAG chunk 列表，用格式指纹
        （:func:`_is_stale_table_chunk`）统计旧格式表格 chunk 残留。
        reparse_strict 删除旧 doc 失败时新旧格式共存，本扫描是唯一可观测通道。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)

        result = await get_rag_client().get_doc_chunks(
            document_id=document_id,
            knowledge_base_id=doc.knowledge_base_id,
            tenant_id=tenant_id,
        )
        chunks = result.get("chunks") or []
        stale = []
        for c in chunks:
            is_stale, reason = _is_stale_table_chunk(
                c.get("content", ""), c.get("file_path", ""),
            )
            if is_stale:
                stale.append({
                    "chunk_id": c.get("chunk_id"),
                    "reason": reason,
                    "preview": (c.get("content") or "")[:120],
                })
        return {
            "doc_id": document_id,
            "total": len(chunks),
            "stale_chunk_count": len(stale),
            "stale_chunks": stale,
        }

    async def purge_stale_chunks(self, tenant_id: str, document_id: str) -> dict:
        """[jonex] §table-grid-v2 O7: 按格式指纹定点清理旧格式 chunk。

        避免整篇重推（reparse 的代价是 LLM 抽取全量重跑）；只删命中指纹的
        chunk（chunk 即 LightRAG doc，逐 chunk_id delete）。并发上限 5。
        """
        tenant_id = require_tenant(tenant_id)
        scan = await self.scan_stale_chunks(tenant_id, document_id)
        stale_ids = [
            s.get("chunk_id") for s in scan.get("stale_chunks", [])
            if s.get("chunk_id")
        ]
        if not stale_ids:
            return {
                "doc_id": document_id,
                "purged": 0,
                "failed": 0,
                "total": scan.get("total", 0),
            }

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)

        rag = get_rag_client()
        sem = asyncio.Semaphore(5)
        results: list[tuple[str, bool]] = []

        async def _purge_one(chunk_id: str) -> None:
            async with sem:
                try:
                    # [jonex] O7 主路径：孤儿清理（同步删 text_chunks/向量/
                    # full_docs，返回真实结果）。doc 级删除是 fire-and-forget
                    # （HTTP 立即返回 deletion_started，后台 not found 无感知），
                    # 不能作为定点清理的判据。
                    ok = await rag.delete_orphan_chunk(
                        chunk_id, tenant_id,
                        knowledge_base_id=doc.knowledge_base_id,
                    )
                    results.append((chunk_id, bool(ok)))
                except Exception as exc:  # noqa: BLE001 — 单 chunk 失败不阻断批量
                    logger.warning(
                        "O7 purge-stale chunk %s failed: %s", chunk_id, exc,
                    )
                    results.append((chunk_id, False))

        await asyncio.gather(*[_purge_one(cid) for cid in stale_ids])
        failed = [cid for cid, ok in results if not ok]
        return {
            "doc_id": document_id,
            "purged": len(stale_ids) - len(failed),
            "failed": len(failed),
            "failed_chunk_ids": failed,
            "total": scan.get("total", 0),
        }
        """[jonex] OpenKB 文档的 chunk 视图：读共享卷 parsed markdown 按块返回。

        OpenKB 管线只解析产出 markdown（parse_only），不入 LightRAG，因此 chunks
        不在 LightRAG。解析产物由 atomic-rag 写到共享卷 inputs/parsed/{doc}/content.md
        （knowledge-base 亦挂载该卷于 KB_INPUT_DIR），这里直接读取并按空行分块返回，
        与 LightRAG chunks 返回结构对齐（content + chunk_index + 位置占位）。
        """
        import os
        from pathlib import Path

        inputs_root = os.getenv("KB_INPUT_DIR", "/app/inputs")
        md_path = Path(inputs_root) / "parsed" / document_id / "content.md"
        if not md_path.is_file():
            return {"doc_id": document_id, "total": 0, "chunks": [], "kb_type": "openkb"}

        text = md_path.read_text(encoding="utf-8")
        blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
        chunks = [
            {
                "chunk_id": f"{document_id}#openkb-{i}",
                "content": b,
                # 与 LightRAG chunk 结构对齐：前端解析结果页 getChunkTitle 会读取
                # content_summary 与 file_path，缺字段会 `.match()` 到 undefined 而崩页。
                # OpenKB 无 file_source 锚点，file_path 仅给出 doc= 归属锚点。
                "content_summary": b,
                "file_path": f"doc={document_id}",
                "chunk_index": i,
                "time_start": None,
                "time_end": None,
                "page_no": None,
                "char_start": None,
                "char_end": None,
            }
            for i, b in enumerate(blocks)
        ]
        return {
            "doc_id": document_id,
            "total": len(chunks),
            "chunks": chunks,
            "kb_type": "openkb",
        }

    async def get_chunk(self, tenant_id: str, document_id: str, chunk_id: str) -> dict:
        """按 chunk_id 精确直查单片内容（直连 LightRAG text_chunks，不拉整篇列表）。

        校验 doc 租户归属后，按 chunk_id 直查；再用 chunk 的 file_path `doc=` 锚点校验其确实
        归属于 document_id（防跨文档串取）。未命中抛 ResourceNotFoundError。只认 chunk_id。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)  # 租户+归属校验

        # ── [jonex] OpenKB 管线：从共享卷 parsed markdown 取指定块 ──
        if "#openkb-" in chunk_id:
            listing = self._get_openkb_chunks(document_id)
            for c in listing.get("chunks", []):
                if c["chunk_id"] == chunk_id:
                    return {"doc_id": document_id, "chunk_id": chunk_id,
                            "content": c["content"], "chunk_index": c["chunk_index"]}
            raise ResourceNotFoundError(
                message=translate("err.kb.chunk_not_found", params={"chunk_id": chunk_id},
                                  fallback=f"未找到 chunk: {chunk_id}"),
            )

        chunk = await get_rag_client().get_chunk_by_id(
            chunk_id=chunk_id,
            knowledge_base_id=doc.knowledge_base_id,
            tenant_id=tenant_id,
        )
        if not chunk:
            raise ResourceNotFoundError(
                message=translate("err.kb.chunk_not_found", params={"chunk_id": chunk_id},
                                  fallback=f"未找到 chunk: {chunk_id}"),
            )
        # 归属校验：chunk 的 file_path doc= 锚点须等于 document_id，避免跨文档串取
        parsed = parse_file_source(chunk.get("file_path", "")) or {}
        if parsed.get("doc_id") and parsed["doc_id"] != document_id:
            raise ResourceNotFoundError(
                message=translate("err.kb.chunk_not_found", params={"chunk_id": chunk_id},
                                  fallback=f"未找到 chunk: {chunk_id}"),
            )
        # 去除入库时注入的命名空间隔离标记 <!--yx:HASH-->，与 references 文本口径一致，不泄露给前端
        import re
        content = re.sub(
            r"\s*<!--yx:[0-9a-f]+-->\s*", "", chunk.get("content") or "",
        ).strip()
        return {
            "doc_id": document_id,
            "kb_id": doc.knowledge_base_id,
            "file_name": doc.file_name,
            "chunk_id": chunk.get("chunk_id") or chunk_id,
            "chunk_index": chunk.get("chunk_order_index"),
            "content": content,
            "page_idx": chunk.get("page_idx"),
            "line_start": chunk.get("line_start"),
            "line_end": chunk.get("line_end"),
        }

    async def reparse_document(
        self, tenant_id: str, document_id: str, *,
        user_id: Optional[str] = None, username: Optional[str] = None, ip: Optional[str] = None,
        force: bool = False,
    ) -> dict:
        """按文档 id 重新解析。

        复用文档已存的 storage_key / storage_backend / 所属 KB，重新走"按文件类型选 preset
        + 推送 compiled ontology schema"链路，经 atomic-rag `retry` action 强制重解析。
        与上传一致：preset 解析不到 → 文档标记解析失败、保留记录、不推送。

        [jonex] R1-c：默认跳过源文件未变化的 reparse（比对内容 md5）。
        传 force=True 可绕过 hash 检查、强制重跑（适应解析配置变更、本体规则调整等场景）。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
            # [jonex] P0-I 互斥：正在解析/清理中（PARSING/PENDING）或删除中的文档拒绝重复 reparse，
            # 避免两个 reparse 并发用不同 old_ids 快照互删、旧代次覆盖新代次。
            if doc.status in (DocStatus.PARSING.value, DocStatus.PENDING.value, DocStatus.INGESTING.value):
                raise ResourceConflictError(message=translate("err.doc.parsing_or_cleaning", fallback="文档正在解析/入库中，请等待完成后再重新解析")  )  # 原消息)
            if doc.status == DocStatus.DELETING.value:
                raise ResourceConflictError(message=translate("err.doc.deleting_cannot_reparse", fallback="文档正在删除中，无法重新解析")  )  # 原消息)
            # [jonex] P1：检查是否已有 in-flight 的 RAG 解析任务，防止短时间内重复提交
            # 导致 old_ids 刚写入就被新 reparse 要求删除（cleanup ↔ index 竞争）。
            # 即使 KB 侧状态已是 READY/FAILED，只要 RAG 任务仍在跑就不允许新建第二个。
            if doc.rag_task_id:
                try:
                    task_info = await get_rag_client().get_task_status(
                        doc.rag_task_id, tenant_id,
                    )
                    task_status = str((task_info or {}).get("status", "")).lower()
                    if task_status in ("created", "queued", "processing"):
                        raise ResourceConflictError(
                            message=translate(
                                "err.doc.reparse_in_flight",
                                fallback="该文档已有正在执行的解析任务，请等待完成后再重新解析",
                            )
                        )
                except ResourceConflictError:
                    raise
                except Exception as exc:
                    # fail-open：查询失败不阻塞正常 reparse，仅打日志
                    logger.warning(
                        "P1 reparse idempotency check failed for doc=%s task=%s: %s",
                        document_id, doc.rag_task_id, exc,
                    )
            kb_id = doc.knowledge_base_id
            file_path = doc.file_path
            storage_key = doc.storage_key
            storage_backend = (doc.storage_backend or "local").strip().lower()
            file_name = doc.file_name
            # reparse 走严格全量替换：快照旧 rag_doc_ids（权威 old_ids 之一，另一半由 atomic-rag 现查）
            old_rag_doc_ids = list(doc.rag_doc_ids or [])

            # ── [jonex] kb_type 分流：openkb 只重解析产出 markdown，不入 LightRAG/不抽本体 ──
            # 复用当前 session 直查 KnowledgeInfo（避免嵌套开 session）
            from ..models.knowledge_info import KnowledgeInfo

            _pt_row = await session.execute(
                select(KnowledgeInfo.kb_type).where(
                    KnowledgeInfo.tenant_id == tenant_id,
                    KnowledgeInfo.id == kb_id,
                    KnowledgeInfo.is_deleted == 0,
                )
            )
            kb_type = _pt_row.scalar() or "lightrag"
            is_openkb = kb_type == "openkb"

            # [jonex] R1-c：比对源文件内容 md5 + 解析配置指纹，两者都未变化才跳过。
            # 源文件或配置（preset/prompt/schema）任一变更 → 自动放行重解析，无需手动 force。
            # hash 取不到（临时错误）→ fail-open，打 warning 继续走 reparse。
            # [jonex] 优先读 content_hash 列（上传去重持久化的单一事实来源），缺失回退旧 extra_metadata
            stored_hash = (doc.content_hash or "") or (doc.extra_metadata or {}).get("source_content_hash", "")
            stored_cfg_hash = (doc.extra_metadata or {}).get("config_fingerprint", "")
            current_hash = ""
            if not force:
                try:
                    current_hash = await self._compute_source_hash(storage_backend, storage_key)
                except Exception as exc:
                    logger.warning(
                        "R1-c source hash compute failed for doc=%s, proceed with reparse: %s",
                        document_id, exc,
                    )

            # 解析配置指纹：仅在源文件 hash 匹配时才计算（避免无谓开销）
            cfg_hash = ""
            _cached_preset = ""
            _cached_prompt_id = None
            _cached_schema_ver = 0
            if not force and current_hash and stored_hash and current_hash == stored_hash:
                try:
                    cfg_hash, _cached_preset, _cached_prompt_id, _cached_schema_ver = \
                        await self._compute_config_fingerprint(tenant_id, kb_id, file_name)
                except Exception as exc:
                    logger.warning(
                        "Config fingerprint compute failed for doc=%s, proceed with reparse: %s",
                        document_id, exc,
                    )

            # R1-c 跳过条件：源文件 AND 解析配置均未变化
            can_skip = bool(current_hash and stored_hash and current_hash == stored_hash
                            and cfg_hash and stored_cfg_hash and cfg_hash == stored_cfg_hash)
            # [jonex] openkb：源文件+配置未变也不能无条件 skip —— 若 parsed artifact 丢失
            # （卷被清理 / 产物开关曾关闭 / 手工删过），skip 会与「重新编译」的
            # "请先重新解析" 提示形成死循环，用户永远出不去。产物缺失时必须继续解析。
            if can_skip and is_openkb:
                from .openkb_service import openkb_artifact_exists
                if not openkb_artifact_exists(document_id):
                    can_skip = False
                    logger.info(
                        "[jonex] openkb reparse 不跳过：源文件未变但 parsed artifact 缺失 doc=%s",
                        document_id,
                    )
            if can_skip:
                logger.info(
                    "R1-c skip reparse (source+config unchanged): doc=%s hash=%s",
                    document_id, current_hash[:16],
                )
                schedule_emit({
                    "tenant_id": tenant_id,
                    "user_id": _audit_user_id(user_id),
                    "username": username,
                    "ip": ip,
                    "log_type": "TASK",
                    "action": "document.reparse_skipped",
                    "outcome": "SUCCESS",
                    "service_name": "knowledge_base",
                    "resource": ResourceType.DOCUMENT.value,
                    "resource_id": str(document_id),
                    "request_params": {"reason": "source_and_config_unchanged", "source_hash": current_hash[:16]},
                })
                return doc.to_dict()

            # [jonex] P0-I：原子递增 reparse 代次；重置状态（reparse 期间 ontology 置 PENDING，
            # 代次变化会 fencing 掉在途 ontology-only 结果，实现 reparse↔ontology-only 互斥）
            new_generation = (doc.content_generation or 0) + 1
            doc.content_generation = new_generation
            doc.status = DocStatus.PARSING.value
            # [jonex] openkb 不抽本体：显式置 READY（而非仅"不置 PENDING"），
            # 顺带清掉历史脏状态（分流上线前遗留的 PENDING/FAILED），
            # 避免被本体巡检当成待办反复扫。与对账 _handle_completed 的口径一致。
            doc.ontology_status = (
                OntologyStatus.READY.value if is_openkb else OntologyStatus.PENDING.value
            )
            doc.error_message = None
            if is_openkb:
                doc.ontology_error = None
            # [jonex] R1-c：持久化源文件 hash + 配置指纹到 extra_metadata，供后续 reparse 比对
            # [jonex] R2-a0: 清除旧 rag_task_id（防对账用旧 id 查到 not_found→判死）
            # + 写入提交锚点（宽限期从这一刻开始）
            doc.rag_task_id = None
            # [jonex] R1-c：持久化源文件 hash + 配置指纹到 extra_metadata
            extra = dict(doc.extra_metadata or {})
            if current_hash:
                extra["source_content_hash"] = current_hash
                # [jonex] 列回填：content_hash 列是去重查询的单一事实来源
                # （_find_duplicate 只查列）——存量文档列恒 NULL 时去重对它失效。
                # reparse 重算后顺带写列，之后该文档重新纳入同 KB 去重范围
                doc.content_hash = current_hash
            elif not doc.content_hash and extra.get("source_content_hash"):
                # 无 current_hash（force 或 hash 计算失败 fail-open）但 extra 有旧值：
                # 免重算迁移到列（旧值来自上一次成功的 reparse 计算，可信）
                doc.content_hash = extra["source_content_hash"]
            if cfg_hash:
                extra["config_fingerprint"] = cfg_hash
            # [jonex] openkb：reparse 开始时把编译结果标为 stale（旧 Wiki 已过期），
            # 避免页面显示旧编译结果。解析完成后由对账 _handle_completed → _dispatch_openkb_compile 自动重新编译。
            if is_openkb:
                # 列化为主（migration 008）；JSONB 键仍写一份兼容旧读点（迁移期）
                doc.llm_wiki_compile_status = "stale"
                doc.llm_wiki_compile_error = None
                doc.llm_wiki_compile_warnings = None
                # [jonex] 清旧编译 task_id：stale 窗口内读到的 task_id 是陈旧的
                # （对应旧 Wiki 的编译任务）；补提交时 claim_compile 会重写
                doc.llm_wiki_task_id = None
                extra["openkb_compile_status"] = "stale"
                extra.pop("openkb_compile_error", None)
                extra.pop("openkb_compile_warnings", None)
            # [jonex] R2-a0: 提交锚点（同 flush 写入，无额外 DB 写）
            # [jonex] P1-1: 统一用 datetime.now()（naive 本地时间），与 patrol 口径一致
            extra["submit_started_at"] = datetime.now().isoformat()
            # [jonex] P0-3: patrol 计时基准（与 upload_document 同口径）
            extra["parsing_started_at"] = datetime.now().isoformat()
            doc.extra_metadata = extra
            await session.flush()
            doc_dict = doc.to_dict()

        uid = _audit_user_id(user_id)
        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": uid,
            "username": username,
            "ip": ip,
            "log_type": "TASK",
            "action": "document.reparse",
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(document_id),
        })

        # COS 后端：确认对象仍存在
        if storage_backend == "cos":
            exists = await get_object_storage().head_object(storage_key)
            if not exists:
                # [jonex] 文档此前已置 PARSING，这里直接 raise 会永久卡住（PARSING 又被
                # 入口互斥拒绝，用户无法重试）。先回写 FAILED 再抛。
                err = translate("err.cos.object_deleted", params={"storage_key": storage_key},
                                fallback=f"COS 对象不存在或已被删除: {storage_key}")
                try:
                    async with get_db_session() as session:
                        repo = KnowledgeDocumentRepository(session)
                        _doc = await repo.get_required(document_id, tenant_id)
                        await repo.set_status(_doc, DocStatus.FAILED, error_message=err)
                except Exception:
                    logger.warning("[jonex] COS 对象缺失但回写 FAILED 失败 doc=%s", document_id, exc_info=True)
                raise ResourceNotFoundError(message=err, details={"storage_key": storage_key})

        # 确保 KB 编译 schema 存在（非阻塞）
        # 若已在指纹计算阶段解析过，优先复用缓存值
        schema = None
        # [jonex] openkb 不用 compiled schema；且 auto_compile=True 有副作用（凭空生成 schema），
        # 必须整段跳过，不能只是取了不传。
        schema_version = _cached_schema_ver if (not is_openkb and _cached_schema_ver) else 0
        if not is_openkb:
            try:
                from .ontology_compiler import OntologyCompiler
                schema = await OntologyCompiler().get_compiled_schema(tenant_id, kb_id, auto_compile=True)
                if schema:
                    schema_version = int(schema.get("schema_version", 0) or 0)
            except Exception as exc:
                logger.warning("Failed to ensure compiled schema for KB %s: %s", kb_id, exc)

        try:
            preset = _cached_preset if _cached_preset else None
            prompt_config_id = _cached_prompt_id
            if not preset:
                preset, prompt_config_id = await self._resolve_parser_preset(tenant_id, kb_id, file_name)
            # [jonex] 主解析提示词下发：重解析同样带上当前配置
            prompt_ids = [prompt_config_id] if prompt_config_id else []
            # [jonex] openkb：parse_only（只解析产出 markdown，不入 LightRAG、不抽本体）；
            # 相应地不需要 strict_push / old_rag_doc_ids（LightRAG 里本就没有该文档的 doc）。
            # [jonex] R1：生成确定性幂等键（reparse 代次递增 → 天然是新键）
            idempotency_key = f"insert:{tenant_id}:{kb_id}:{document_id}:{new_generation}"
            rag_result = await get_rag_client().retry(
                file_path=file_path,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                document_id=document_id,
                ontology_schema=schema,            # openkb 下恒为 None
                storage_backend=storage_backend,
                storage_key=storage_key,
                preset=preset,
                prompt_ids=prompt_ids,
                execution_mode="parse_only" if is_openkb else "reparse_strict",
                strict_push=not is_openkb,
                content_generation=new_generation,
                schema_version=schema_version,     # openkb 下恒为 0
                old_rag_doc_ids=[] if is_openkb else old_rag_doc_ids,
                idempotency_key=idempotency_key,  # [jonex] R1
            )
        except Exception as exc:
            logger.exception("Knowledge document reparse failed: %s", document_id)
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                doc = await repo.get_required(document_id, tenant_id)
                await repo.set_status(doc, DocStatus.FAILED, error_message=str(exc))
                # [jonex] R2-a0: 清除提交锚点
                # [jonex] N1: SQLAlchemy JSON 列不追踪原地 pop，必须整体重赋值
                meta = dict(doc.extra_metadata or {})
                meta.pop("submit_started_at", None)
                doc.extra_metadata = meta
                doc_dict = doc.to_dict()
            schedule_emit({
                "tenant_id": tenant_id,
                "user_id": uid,
                "username": username,
                "ip": ip,
                "log_type": "TASK",
                "action": "document.parse_failed",
                "outcome": "FAILED",
                "service_name": "knowledge_base",
                "resource": ResourceType.DOCUMENT.value,
                "resource_id": str(document_id),
                "error_message": str(exc)[:1000],
            })
            return doc_dict

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
            status = DocStatus.READY if not rag_result.get("task_id") else DocStatus.PARSING
            await repo.set_status(
                doc,
                status,
                rag_task_id=rag_result.get("task_id"),
                rag_doc_ids=rag_result.get("doc_ids") or rag_result.get("document_ids") or [],
            )
            # [jonex] P1-E：reparse 也写目标 schema 版本
            if schema_version:
                doc.ontology_target_schema_version = schema_version
            doc.extra_metadata = {**(doc.extra_metadata or {}), "rag_result": rag_result}
            # [jonex] R2-a0: 清除提交锚点
            doc.extra_metadata.pop("submit_started_at", None)
            doc_dict = doc.to_dict()
        return doc_dict

    async def list_documents(self, tenant_id: str, request: DocumentListRequest | dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = DocumentListRequest(**_payload(request))
        offset = (req.page - 1) * req.page_size

        # [jonex] openkb KB：编译 phase 按 llm_wiki_compile_status 过滤（_OPENKB_PHASE_PREDICATE），
        # 否则 ontology_status 恒 READY → compiled 全命中 / compiling、compile_failed 筛不到
        kb_type = await self._get_kb_type(tenant_id, req.knowledge_base_id)
        predicates = _OPENKB_PHASE_PREDICATE if kb_type == "openkb" else _PHASE_PREDICATE

        conditions = []
        phase_cond = _phase_condition(req.phase, predicates)
        if phase_cond is not None:
            # phase 优先：线性状态多选，翻译为 (status, ontology_status) 谓词
            conditions.append(phase_cond)
        else:
            if req.status:
                conditions.append(KnowledgeDocument.status == req.status)
            if req.ontology_status:
                conditions.append(KnowledgeDocument.ontology_status == req.ontology_status)
        if req.keyword:
            pattern = f"%{req.keyword}%"
            conditions.append(
                or_(
                    KnowledgeDocument.file_name.ilike(pattern),
                    KnowledgeDocument.file_path.ilike(pattern),
                )
            )
        if req.folder_id == UNCLASSIFIED_SENTINEL:
            conditions.append(KnowledgeDocument.folder_id.is_(None))
        elif req.folder_id:
            conditions.append(KnowledgeDocument.folder_id == req.folder_id)
        conditions.append(KnowledgeDocument.knowledge_base_id == req.knowledge_base_id)

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            items = await repo.list_all(
                tenant_id=tenant_id,
                offset=offset,
                limit=req.page_size,
                extra_conditions=conditions,
            )
            total = await repo.count(tenant_id=tenant_id, extra_conditions=conditions)

        return {
            "items": [item.to_dict() for item in items],
            "total": total,
            "page": req.page,
            "page_size": req.page_size,
        }

    async def documents_stats(self, tenant_id: str, request: DocumentScopeRequest | dict) -> dict:
        """按线性状态口径统计某 KB 文档数（互斥四桶，设计 §5）。

        返回 processing / completed / compile_failed / parse_failed 四个**互斥**桶，
        total = 四桶之和（恒等可对账，deleting/deleted 不计入）。与列表徽章/筛选同源。
        count 由 BaseRepository 统一加租户 + is_deleted=0。
        """
        tenant_id = require_tenant(tenant_id)
        req = DocumentScopeRequest(**_payload(request))
        base = [KnowledgeDocument.knowledge_base_id == req.knowledge_base_id]

        # [jonex] openkb KB：四桶按 llm_wiki_compile_status 判定（ontology_status 恒 READY
        # 不可用）；lightrag 维持 ontology_status 语义。与 _OPENKB_PHASE_PREDICATE 同源。
        kb_type = await self._get_kb_type(tenant_id, req.knowledge_base_id)
        is_openkb = kb_type == "openkb"

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)

            async def _count(extra: list) -> int:
                return await repo.count(tenant_id=tenant_id, extra_conditions=base + extra)

            # 互斥四桶（相加 = total）：每个文档恰好落进一桶，避免嵌套指标的对账困惑。
            # 处理中：待解析/解析中/入库中/待编译/编译中（还在跑）
            if is_openkb:
                processing = await _count([
                    or_(
                        KnowledgeDocument.status.in_([
                            DocStatus.PENDING.value,
                            DocStatus.PARSING.value,
                            DocStatus.INGESTING.value,
                        ]),
                        and_(
                            KnowledgeDocument.status == DocStatus.READY.value,
                            or_(
                                KnowledgeDocument.llm_wiki_compile_status.is_(None),
                                KnowledgeDocument.llm_wiki_compile_status == "stale",
                                KnowledgeDocument.llm_wiki_compile_status == "compiling",
                            ),
                        ),
                    )
                ])
                completed = await _count([
                    and_(
                        KnowledgeDocument.status == DocStatus.READY.value,
                        KnowledgeDocument.llm_wiki_compile_status == "compiled",
                    )
                ])
                compile_failed = await _count([
                    and_(
                        KnowledgeDocument.status == DocStatus.READY.value,
                        KnowledgeDocument.llm_wiki_compile_status == "failed",
                    )
                ])
            else:
                processing = await _count([
                    or_(
                        KnowledgeDocument.status.in_([
                            DocStatus.PENDING.value,
                            DocStatus.PARSING.value,
                            DocStatus.INGESTING.value,
                        ]),
                        and_(
                            KnowledgeDocument.status == DocStatus.READY.value,
                            KnowledgeDocument.ontology_status.in_([
                                OntologyStatus.PENDING.value,
                                OntologyStatus.EXTRACTING.value,
                            ]),
                        ),
                    )
                ])
                # 已完成：解析+图谱都就绪
                completed = await _count([
                    and_(
                        KnowledgeDocument.status == DocStatus.READY.value,
                        KnowledgeDocument.ontology_status == OntologyStatus.READY.value,
                    )
                ])
                # 编译失败：可搜索但图谱未建成
                compile_failed = await _count([
                    and_(
                        KnowledgeDocument.status == DocStatus.READY.value,
                        KnowledgeDocument.ontology_status == OntologyStatus.FAILED.value,
                    )
                ])
            # 解析失败：不可用（两类型语义一致）
            parse_failed = await _count([KnowledgeDocument.status == DocStatus.FAILED.value])

        # total 定义为四桶之和，恒等可对账（deleting/deleted 不计入）。
        total = processing + completed + compile_failed + parse_failed
        return {
            "total": total,
            "processing": processing,
            "completed": completed,
            "compile_failed": compile_failed,
            "parse_failed": parse_failed,
        }

    async def get_document(
        self,
        tenant_id: str,
        document_id: str,
        request: DocumentScopeRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = DocumentScopeRequest(**_payload(request))
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_by_id(document_id, tenant_id)
            if doc is None or doc.knowledge_base_id != req.knowledge_base_id:
                raise ResourceNotFoundError(message=translate("err.doc.not_found", fallback="知识文档不存在")  )  # 原消息)
            return doc.to_dict()

    async def get_document_status(
        self,
        tenant_id: str,
        document_id: str,
        kb_ids: list[str],
    ) -> dict:
        """查询文档处理状态，供 MCP 上传进度追踪使用。

        轻量级状态查询，校验文档归属在授权的 KB 范围内。不返回完整的 to_dict()，
        仅返回与上传/解析进度监控相关的字段。

        Args:
            tenant_id: 租户 ID。
            document_id: 文档 ID（由 upload_document 返回）。
            kb_ids: MCP Key 授权访问的 KB ID 列表，文档所属 KB 必须在其中。

        Returns:
            dict: 状态字段（doc_id、status、ontology_status、error_message、
                  file_name、file_size、knowledge_base_id、created_at、updated_at）。

        Raises:
            ResourceNotFoundError: 文档不存在或 KB 不在授权范围内。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_by_id(document_id, tenant_id)
            if doc is None:
                raise ResourceNotFoundError(
                    message=translate("err.doc.not_found", fallback="知识文档不存在"),
                )
            if doc.knowledge_base_id not in kb_ids:
                raise ResourceNotFoundError(
                    message=translate("err.doc.not_found", fallback="知识文档不存在"),
                )
            return {
                "doc_id": doc.id,
                "status": doc.status,
                "ontology_status": doc.ontology_status,
                "error_message": doc.error_message,
                "file_name": doc.file_name,
                "file_size": doc.file_size,
                "knowledge_base_id": doc.knowledge_base_id,
                "created_at": doc.created_at.isoformat(),
                "updated_at": doc.updated_at.isoformat(),
            }

    async def delete_document(
        self,
        tenant_id: str,
        document_id: str,
        request: DocumentScopeRequest | dict,
        *,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = DocumentScopeRequest(**_payload(request))
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_by_id(document_id, tenant_id)
            if doc is None or doc.knowledge_base_id != req.knowledge_base_id:
                raise ResourceNotFoundError(message=translate("err.doc.not_found", fallback="知识文档不存在")  )  # 原消息)
            if doc.status == DocStatus.DELETING.value:
                raise ResourceConflictError(message=translate("err.doc.deleting_in_progress", fallback="知识文档正在删除中")  )  # 原消息)
            if doc.status in (DocStatus.PENDING.value, DocStatus.PARSING.value, DocStatus.INGESTING.value):
                raise ResourceConflictError(message=translate("err.doc.parsing_cannot_delete", fallback="知识文档正在解析/入库中，请等待完成后再删除")  )  # 原消息)
            await repo.set_status(doc, DocStatus.DELETING)
            rag_doc_ids = list(doc.rag_doc_ids or [])
            kb_id = doc.knowledge_base_id or ""
            file_name = doc.file_name or ""
            storage_key = doc.storage_key
            storage_backend = doc.storage_backend

        # rag_doc_ids 可能为空（insert 不返回 doc_ids，对账未及时补充时），
        # 此时从 LightRAG 存储反查，确保已入库的内容能被清理。
        if not rag_doc_ids and file_name and kb_id:
            rag_doc_ids = await self._lookup_rag_doc_ids(
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                file_name=file_name,
                document_id=document_id,
            )

        if rag_doc_ids:
            logger.info(
                "Deleting %d LightRAG documents for doc %s: %s",
                len(rag_doc_ids), document_id, rag_doc_ids[:10],
            )
            # [jonex] 批量删除：单次 DELETE 提交全部 doc_ids，LightRAG
            # 在单个 background_delete_documents 内批量处理，批末仅一次
            # rebuild_knowledge_from_chunks，避免逐条删除时每个 doc 各自
            # 触发 LLM summarization。
            try:
                result = await get_rag_client().delete_batch(
                    rag_doc_ids, tenant_id=tenant_id, knowledge_base_id=kb_id,
                    document_id=document_id,
                )
                accepted = result.get("accepted", [])
                failed = result.get("failed", [])
                if accepted:
                    logger.info(
                        "RAG batch delete accepted %d/%d doc_ids for doc %s",
                        len(accepted), len(rag_doc_ids), document_id,
                    )
                if failed:
                    logger.warning(
                        "RAG batch delete failed %d doc_ids for doc %s: %s",
                        len(failed), document_id, failed,
                    )
            except Exception:
                logger.warning(
                    "RAG batch delete failed for doc %s", document_id, exc_info=True,
                )
        else:
            logger.warning(
                "No LightRAG doc_ids found for document %s (file_name=%s), "
                "LightRAG cleanup will be skipped",
                document_id, file_name,
            )

        # ── [jonex] kb_type 分流 ──
        kb_type = await self._get_kb_type(tenant_id, kb_id)

        if kb_type == "openkb":
            from .openkb_service import KnowledgeCompilerService  # [jonex] lazy import 避免循环依赖
            compiler = KnowledgeCompilerService()
            try:
                await compiler.remove_document(
                    kb_name=kb_id, document_id=document_id,
                    tenant_id=tenant_id, kb_id=kb_id,
                )
            except Exception:
                logger.warning("Failed to remove OpenKB document %s", document_id, exc_info=True)
        else:
            for rag_doc_id in rag_doc_ids:
                try:
                    await get_rag_client().delete(
                        rag_doc_id, tenant_id=tenant_id, knowledge_base_id=kb_id
                    )
                except Exception:
                    logger.warning("Failed to delete RAG document %s", rag_doc_id, exc_info=True)

        # 清理 Neo4j 本体图谱中该文档关联的实体节点和关系
        # [jonex] openkb 文档从不写 Neo4j（parse_only 管线），跳过——纯多余调用
        # （每次删除省一次 Neo4j 往返；Neo4j 不可用时也不再产生无关 warning）
        if kb_type != "openkb":
            try:
                gdao = OntologyGraphRepository(get_neo4j_driver())
                await gdao.delete_by_document(tenant_id, document_id)
            except Exception:
                logger.warning("Neo4j cleanup failed for document %s", document_id, exc_info=True)

        # 清理对象存储中的原始上传文件（COS / Local）
        if storage_key:
            try:
                storage = get_object_storage_for(storage_backend) if storage_backend else get_object_storage()
                await storage.delete(storage_key)
                logger.info("Deleted storage file %s for doc %s", storage_key, document_id)
            except Exception:
                logger.warning("Failed to delete storage file for document %s", document_id, exc_info=True)

        async with get_db_session() as session:
            # 显式清理文档-标签关联（软删除不触发外键 CASCADE）
            try:
                await session.execute(
                    delete(DocumentTag).where(DocumentTag.document_id == document_id)
                )
            except Exception:
                logger.warning("Failed to clean document_tags for doc %s", document_id, exc_info=True)

            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
            await repo.set_status(doc, DocStatus.DELETED)
            await repo.delete_soft(doc, tenant_id)

        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": _audit_user_id(user_id),
            "username": username,
            "ip": ip,
            "log_type": "OPERATION",
            "action": "document.delete",
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(document_id),
        })

        return {"id": document_id, "deleted": True}

    async def set_document_folder(
        self, tenant_id: str, document_id: str, req: SetDocumentFolderRequest | dict
    ) -> dict:
        """设置或清除文档的文件夹归属。

        如果 folder_id 非空，校验文件夹属于同一个 knowledge_base_id；
        如果 folder_id 为 None，清除文档的文件夹归属。
        """
        tenant_id = require_tenant(tenant_id)
        data = _payload(req)
        folder_id = data.get("folder_id")
        knowledge_base_id = data.get("knowledge_base_id")

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_by_id(document_id, tenant_id)
            if doc is None or doc.knowledge_base_id != knowledge_base_id:
                raise ResourceNotFoundError(message=translate("err.doc.not_found", fallback="知识文档不存在")  )  # 原消息)

            if folder_id:
                # 校验文件夹存在且属于同一个 knowledge_base_id
                folder_repo = FolderRepository(session)
                folder = await folder_repo.get_required(folder_id, tenant_id)
                if folder.knowledge_base_id != knowledge_base_id:
                    raise ResourceNotFoundError(
                        message=translate("err.folder.not_belong_to_kb", fallback="文件夹不属于该知识库")  ,  # 原消息
                        details={"folder_id": folder_id, "knowledge_base_id": knowledge_base_id},
                    )

            doc.folder_id = folder_id
            await session.commit()
            return doc.to_dict()

    async def batch_set_document_folder(
        self, tenant_id: str, req: BatchMoveDocumentsRequest | dict
    ) -> dict:
        """批量设置文档文件夹归属（整体事务回滚 + 幂等跳过）。

        - folder_id 非空：校验目标目录存在且属于同一 KB，否则整批拒绝；
        - folder_id 为 None：批量移出到「未分类」；
        - 已在目标目录的文档计入 skipped_count，不报错；
        - 任一文档不存在/跨 KB 时整批回滚，details.document_id 指出具体项。
        """
        tenant_id = require_tenant(tenant_id)
        data = _payload(req)
        knowledge_base_id = data["knowledge_base_id"]
        folder_id = data.get("folder_id")
        document_ids = list(dict.fromkeys(data["document_ids"]))  # 保序去重

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)

            # 目标目录校验（非空才校验；None = 移出到未分类）
            if folder_id:
                folder = await FolderRepository(session).get_required(folder_id, tenant_id)
                if folder.knowledge_base_id != knowledge_base_id:
                    raise ResourceNotFoundError(
                        message=translate("err.folder.not_belong_to_kb", fallback="文件夹不属于该知识库"),
                        details={"folder_id": folder_id, "knowledge_base_id": knowledge_base_id},
                    )

            moved = 0
            skipped = 0
            for doc_id in document_ids:
                doc = await repo.get_by_id(doc_id, tenant_id)  # get_by_id 已过滤 is_deleted=0
                if doc is None or doc.knowledge_base_id != knowledge_base_id:
                    raise ResourceNotFoundError(
                        message=translate("err.doc.not_found", fallback="知识文档不存在"),
                        details={"document_id": doc_id, "knowledge_base_id": knowledge_base_id},
                    )
                if doc.folder_id == folder_id:
                    skipped += 1
                    continue
                doc.folder_id = folder_id
                moved += 1

            await session.commit()
            return {"moved_count": moved, "skipped_count": skipped, "folder_id": folder_id}

    async def _lookup_rag_doc_ids(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        file_name: str,
        document_id: str,
    ) -> list[str]:
        """从 LightRAG 存储反查文档的实际 doc_ids。

        用于 rag_doc_ids 为空时的兜底（insert 不返回 doc_ids，对账可能未及时补充）。
        优先通过 file_path 中的 doc=<id> 匹配，再降级到 file_name 匹配。
        """
        import re

        try:
            result = await get_rag_client().get_storage_documents(
                knowledge_base_id=knowledge_base_id,
                tenant_id=tenant_id,
                keyword=file_name,
                page=1,
                page_size=500,
            )
        except Exception as e:
            logger.warning(
                "Storage lookup failed for doc %s during delete: %s", document_id, e
            )
            return []

        items = result.get("items", [])
        total = result.get("total", len(items))
        if total > len(items):
            logger.warning(
                "Storage fallback during delete: total %d exceeds page size for doc %s",
                total, document_id,
            )

        matched = []
        for item in items:
            fp = item.get("file_path") or ""
            m = re.search(r'doc=([a-f0-9-]+)\|', fp)
            if m and m.group(1) == document_id:
                matched.append(item)
        if not matched:
            matched = [
                item for item in items
                if (
                    item.get("file_name") == file_name
                    or (item.get("file_name") or "").endswith("_" + file_name)
                )
            ]

        if not matched:
            logger.info(
                "Storage fallback miss during delete: doc_id=%s, file_name=%s, total=%d",
                document_id, file_name, len(items),
            )
            return []

        rag_doc_ids = [item["id"] for item in matched if item.get("id")]
        logger.info(
            "Storage fallback hit during delete: doc_id=%s, found %d LightRAG doc_ids",
            document_id, len(rag_doc_ids),
        )
        return rag_doc_ids

    # ── [jonex] kb_type 查询 → 公共 helper ──

    async def _get_kb_type(self, tenant_id: str, kb_id: str) -> str:
        """[jonex] 查询 KB 的 kb_type（转调 kb_type_service）。"""
        from .kb_type_service import get_kb_type
        return await get_kb_type(tenant_id, kb_id)


__all__ = ["DocumentService"]
