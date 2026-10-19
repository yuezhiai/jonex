"""Document application service for Knowledge Base."""

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote
from uuid import uuid4

from neo4j.exceptions import DriverError, TransientError
from sqlalchemy import and_, delete, or_, select, text

from jonex_core.capability.atomic.rag.client import get_rag_client
from jonex_core.common.audit import schedule_emit
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.database import get_db_session
from jonex_core.common.file_source_util import classify_media, parse_file_source
from jonex_core.common.exceptions import (
    InternalError,
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
    build_preview_key,  # [jonex] office 查看原文定位：preview.pdf 对象键
    get_object_storage,
    get_object_storage_for,
)
from jonex_core.common.tenant import require_tenant

from . import ingest_backend  # [jonex] 入库后端（提交时定 / 查询时读粘性值）
from . import task_intent  # [jonex] 第二个粘性值：这次是哪种操作（upload / reparse）
from .kb_type_service import LLM_WIKI_TYPES  # [jonex] llm-wiki 子集 {openkb, jonex_wiki}

from ..models import (
    DocStatus,
    DocumentTag,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    OntologyStatus,
)
from ..models.data_source import KnowledgeDataSource
from ..models.task_center import TaskCenterDocState, TaskCenterDocument
from ..ontology_write_state import clear_ontology_write_state
from ..repository import (
    TASK_STATUS_FAILED,
    TASK_STATUS_RUNNING,
    TASK_TYPE_PARSE,
    FolderRepository,
    KnowledgeDocumentRepository,
    KnowledgeDocumentVersionRepository,
    OntologyGraphRepository,
    UNCLASSIFIED_SENTINEL,
    record_processing_task,
    safe_record_asset_status_event,
)
from ..repository.knowledge_info_repository import KnowledgeInfoRepository
from ..repository.task_center_repository import TaskCenterDocumentRepository
from ..dtos import (
    BatchMoveDocumentsRequest,
    DocumentListRequest,
    DocumentScopeRequest,
    DocumentUploadRequest,
    SetDocumentFolderRequest,
)
from .quota_service import QuotaService
from .rag_storage_lookup import find_rag_doc_ids, find_rag_doc_ids_by_document
from jonex_core.common.quota import classify_quota_category

logger = logging.getLogger(__name__)


# [jonex] COS 预签名直传单文件大小上限（字节）。默认 500MB 对齐 Nginx 对外硬上限
# client_max_body_size 500m，可通过环境变量 OBJECT_STORAGE_MAX_UPLOAD_SIZE 调整。
_MAX_UPLOAD_SIZE = int(os.getenv("OBJECT_STORAGE_MAX_UPLOAD_SIZE", str(500 * 1024 * 1024)))

# [jonex] 文档本体溯源（数据治理血缘）对外契约上限：limit 1..200。
_PROVENANCE_LIMIT_MAX = 200


# [jonex] 线性状态（phase）→ SQLAlchemy 谓词。单一事实来源，供 list_documents 与
# documents_stats 复用。与前端 deriveDocPhase / 设计文档 §4.1 表一致。
_PHASE_PREDICATE = {
    # 第 3 期生命周期：归档是「可恢复的下线」，默认不进列表（见 list_documents）；
    # 显式用 phase=archived 筛选时才保留。
    "archived":        lambda: KnowledgeDocument.status == DocStatus.ARCHIVED.value,
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


def _provenance_text(value) -> str:
    """输出契约只允许字符串：None → ""，其余非字符串按 ``str()`` 归一。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _parse_provenance_rows(
    entities: list[dict], relations: list[dict],
) -> tuple[list[dict], list[dict]]:
    """归一 Neo4j 溯源原始行。

    ``source_chunks`` 写入侧是 JSON 字符串（``json.dumps`` / ``apoc.convert.toJson``），
    这里必须容忍：JSON 字符串、已是 list、非法 JSON、None、dict、数字 —— 任何输入
    都只能得到 list（否则会把非序列化对象透给上层）。

    去重用于**防御历史/并发写入造成的重复行**（同一实体或边被写重），保序、保留
    首条：实体 ``(canonical_name, entity_type)``，关系 ``(source, target,
    relation_type)``。去重与 ``LIMIT`` 的先后由调用方保证（先全量去重再判截断）。

    ⚠ 关系去重键不含端点 ``entity_type``（输出契约不暴露该字段），因此同名不同型
    的实体在关系里可能被合并为同一条；要区分需先扩输出契约。
    """
    out_entities: list[dict] = []
    seen_entities: set[tuple[str, str]] = set()
    for row in entities or []:
        raw = row.get("source_chunks")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = []
        chunks = raw if isinstance(raw, list) else []
        canonical_name = _provenance_text(row.get("canonical_name"))
        entity_type = _provenance_text(row.get("entity_type"))
        key = (canonical_name, entity_type)
        if key in seen_entities:
            continue
        seen_entities.add(key)
        out_entities.append({
            "canonical_name": canonical_name,
            "entity_type": entity_type,
            "source_chunks": chunks,
        })

    out_relations: list[dict] = []
    seen_relations: set[tuple[str, str, str]] = set()
    for row in relations or []:
        source = _provenance_text(row.get("source"))
        target = _provenance_text(row.get("target"))
        relation_type = _provenance_text(row.get("relation_type"))
        key = (source, target, relation_type)
        if key in seen_relations:
            continue
        seen_relations.add(key)
        out_relations.append({
            "source": source,
            "target": target,
            "relation_type": relation_type,
        })
    return out_entities, out_relations


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


def reset_document_for_rerun(doc: Any, *, is_llm_wiki: bool) -> Dict[str, Any]:
    """把文档行重置成「正在重新解析」，返回**改前快照**（给调用方回滚用）。

    ── 两处共用，口径必须一模一样 ──

    · 用户点「重新解析」→ `DocumentService.reparse_document`；
    · 任务中心的「**任务重试**」→ `task_runner_service._prepare_step` 撞上一张
      已交还（`released`）的接管行 —— 那条任务被原地重跑、又派活来了（见那里的注释）。

    ── ⚠️ 为什么必须共用：这里改的每一列都是「上一轮留下的终态」 ──

    漏掉任何一列，重跑出来的都是**静默的假成功**：

    · `ontology_status` 不回落 `PENDING` → `compile` 步的看门狗一看还是 `READY`
      （那是上一轮的），当场判「本体已就绪 = 编译成功」上报成功，而本体**根本没重抽**；
    · `llm_wiki_compile_status` 不清成 `stale` → 同理，Wiki 没重编也被判成功；
    · `content_generation` 不 +1 → 引擎侧 fencing 失效，在途的旧结果会把新的盖掉。

    ── 只管「状态列 + `extra_metadata` 里那几项『这次重来』的键」 ──

    `source_content_hash` / `config_fingerprint` / `ingest_backend` 这些是
    `reparse_document` 那条路**自己的业务**（它要重算文件 hash、写回后端粘性值）——
    **留在调用点，别搬进来**。

    `tc_intent` 也不在这里写：谁需要「严格重解析」谁自己置。
    """
    rollback = {
        "content_generation": doc.content_generation,
        "status": doc.status,
        "ontology_status": doc.ontology_status,
        "error_message": doc.error_message,
        "ontology_retry_count": doc.ontology_retry_count,
        "ontology_error": doc.ontology_error,
        "rag_task_id": doc.rag_task_id,
        "content_hash": doc.content_hash,
        "llm_wiki_compile_status": doc.llm_wiki_compile_status,
        "llm_wiki_compile_error": doc.llm_wiki_compile_error,
        "llm_wiki_compile_warnings": doc.llm_wiki_compile_warnings,
        "llm_wiki_task_id": doc.llm_wiki_task_id,
        # 新 dict（不是同一引用）：JSONB 的原地改动 SQLAlchemy 认不出来
        "extra_metadata": dict(doc.extra_metadata or {}),
    }

    # [jonex] P0-I：原子递增 reparse 代次；重置状态（reparse 期间 ontology 置 PENDING，
    # 代次变化会 fencing 掉在途 ontology-only 结果，实现 reparse↔ontology-only 互斥）
    doc.content_generation = (doc.content_generation or 0) + 1
    doc.status = DocStatus.PARSING.value
    # [jonex] openkb 不抽本体：显式置 READY（而非仅"不置 PENDING"），
    # 顺带清掉历史脏状态（分流上线前遗留的 PENDING/FAILED），
    # 避免被本体巡检当成待办反复扫。与对账 _handle_completed 的口径一致。
    doc.ontology_status = (
        OntologyStatus.READY.value if is_llm_wiki else OntologyStatus.PENDING.value
    )
    doc.error_message = None
    # [jonex] reparse 是全量重来，本体重试计数与错误一并清零，
    # 否则历史 retry_count 会让对账在重解析后立刻撞上限失败。
    doc.ontology_retry_count = 0
    doc.ontology_error = None
    # [jonex] reparse = 换任务，写图状态一并清零（否则被 Task 3 打了
    # ontology_auto_retry_disabled 的文档在此路径下永久无法恢复）
    doc.extra_metadata = clear_ontology_write_state(doc.extra_metadata)
    # [jonex] R2-a0: 清除旧 rag_task_id（防对账用旧 id 查到 not_found→判死）
    doc.rag_task_id = None
    extra = dict(doc.extra_metadata or {})
    # [jonex] openkb：reparse 开始时把编译结果标为 stale（旧 Wiki 已过期），
    # 避免页面显示旧编译结果。解析完成后由对账 _handle_completed → _dispatch_openkb_compile 自动重新编译。
    if is_llm_wiki:
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
    return rollback


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
        """[jonex] 上传去重：同 KB 内返回内容 hash 相同的活跃文档（未删除且非 failed/archived）。

        返回 row（含 id / file_name）或 None。failed / archived / 已删除的文档不算重复：
        解析失败要能重传，归档是「可恢复的下线」，不应挡住重新上传同一份内容。
        """
        from sqlalchemy import text as _sql_text

        async with get_db_session() as session:
            row = (
                await session.execute(
                    _sql_text(
                        "SELECT id, file_name FROM knowledge_base.knowledge_documents "
                        "WHERE tenant_id = :tid AND knowledge_base_id = :kb "
                        "  AND content_hash = :h "
                        "  AND is_deleted = 0 AND status NOT IN ('failed', 'archived') "
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

        # ── 安全加固：storage_key 租户段 fail-closed 校验 ──
        # 客户端 COS 直传确认会直接信任传入的 storage_key。此处校验 key 的租户/知识库段
        # 与当前上下文一致，防止构造他租户的 storage_key 触发 RAG 解析造成跨租户数据泄露。
        # 兼容三种布局：
        # 1) 旧存量入站推送（api_push，{prefix}/ingest/{ds_id}/{doc_id}/...）：不含租户段，
        #    租户由 ds 记录服务端推导，仅做段数结构校验后放行；
        # 2) 新入站推送（{prefix}/kb/{tenant}/ingest/{ds_id}/{doc_id}/...）：已纳入租户前缀，
        #    校验租户段与上下文一致；
        # 3) 普通 kb 直传/上传（{prefix}/kb/{tenant}/{kb_id}/{doc_id}/...）：校验租户/知识库/文档段。
        if req.storage_key:
            parts = req.storage_key.split("/")
            if len(parts) >= 2 and parts[1] == "ingest":
                # 旧存量入站推送布局：不含租户段，仅做段数结构校验
                valid = len(parts) >= 5
            elif len(parts) >= 4 and parts[1] == "kb" and parts[3] == "ingest":
                # 新入站推送布局：kb/{tenant}/ingest/{ds_id}/{doc_id}/...，校验租户段
                valid = len(parts) >= 6 and parts[2] == tenant_id
            else:
                valid = (
                    len(parts) >= 5
                    and parts[1] == "kb"
                    and parts[2] == tenant_id
                    and parts[3] == req.knowledge_base_id
                )
                if valid and req.doc_id:
                    valid = parts[4] == req.doc_id
            if not valid:
                raise PermissionDeniedError(
                    message="storage_key 与租户/知识库不匹配",
                    details={
                        "storage_key": req.storage_key,
                        "tenant_id": tenant_id,
                        "knowledge_base_id": req.knowledge_base_id,
                    },
                )

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
        if storage_backend == "local":
            global_backend = os.getenv("OBJECT_STORAGE_BACKEND", "local").strip().lower()
            if global_backend in ("cos", "s3"):
                storage_backend = global_backend

        # file_path 由存储后端统一推导（下游 atomic-rag 解析用）：
        #  - 对象存储后端（cos 等）：通过 storage_key 下载，file_path 仅作标识 → 用 storage_key；
        #  - local 后端：解析需可直接读取的绝对路径，由对象存储后端把 key 解析为共享卷绝对路径。
        if storage_backend in ("cos", "s3"):
            file_path = req.file_path or storage_key
        else:
            file_path = get_object_storage().fs_path(storage_key) or req.file_path or storage_key

        # [jonex] 配额：解析真实文件大小 + 静态校验（文件类型 / 单文件大小，事务外）。
        # COS 直传不经 Gateway，前端不填 file_size → 从对象存储元数据（Content-Length）读取。
        file_size = req.file_size
        if storage_backend in ("cos", "s3") and not file_size:
            file_size = await get_object_storage().head_object_size(storage_key) or 0
        quota = QuotaService()
        category = classify_quota_category(req.mime_type, req.file_name)
        quota.check_upload_static(category, file_size)

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
            # [jonex] 配额：计数校验（单 KB 文档数 → 租户文档数 → 租户容量），
            # 与 INSERT 同事务 + advisory lock，校验通过即占额（D2-B）。
            await quota.check_upload_counts(session, tenant_id, req.knowledge_base_id, file_size)
            doc = await repo.create(
                KnowledgeDocument(
                    id=doc_id,
                    tenant_id=tenant_id,
                    file_name=req.file_name,
                    file_path=file_path,
                    file_size=file_size,
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
            # 文档版本链起点：首次上传即 v1（存量由迁移 019/070 回填）
            await KnowledgeDocumentVersionRepository(session).create(
                KnowledgeDocumentVersion(
                    tenant_id=tenant_id,
                    document_id=doc.id,
                    version_no=1,
                    file_name=req.file_name,
                    file_size=file_size,
                    mime_type=req.mime_type,
                    storage_backend=storage_backend,
                    storage_key=storage_key,
                    content_hash=content_hash or None,
                    source="upload",
                    created_by=username or user_id,
                )
            )
            doc_dict = doc.to_dict()
            # [jonex] 数据运营二期：文件解析任务埋点（parse/running）。
            # 不走 `set_status` —— 那里写成「仅状态变化才落」，而这里状态是
            # INSERT 时就带的 PARSING（没有「旧值」可比），必须显式落一次开工。
            # 与 INSERT 同事务，由 `record_processing_task` 内部吞异常。
            await record_processing_task(
                session, tenant_id,
                asset_id=doc.id, task_type=TASK_TYPE_PARSE, status=TASK_STATUS_RUNNING,
                doc_id=doc.id, kb_id=req.knowledge_base_id,
                content_generation=doc.content_generation,
            )

        uid = _audit_user_id(user_id)
        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": uid,
            "username": username,
            "ip": ip,
            "log_type": "OPERATION",
            "action": "upload_document",
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

        # 对象存储后端（cos/s3）：确认对象已存在后再入队解析，避免竞态（D9）
        if storage_backend in ("cos", "s3"):
            exists = await get_object_storage().head_object(storage_key)
            if not exists:
                raise ResourceNotFoundError(
                    message=translate("err.cos.object_not_found", params={"storage_key": storage_key}, fallback=f"COS 对象不存在或上传未完成: {storage_key}")  ,  # 原消息
                    details={"storage_key": storage_key},
                )

        # ── [jonex] kb_type 分流：**提前到这里查** ───
        # 它在下面被用两次、而且第二次比原来更早：① 决定这段要不要算 compiled schema；
        # ② 决定走任务中心还是老路径。原来是挪到 schema 之后才查的，openkb 接进来之后
        # 不够用了（见下）。
        kb_type = await self._get_kb_type(tenant_id, req.knowledge_base_id)
        # ── [jonex] 库级派单值（2026-10-09）：与 kb_type 同点取 ───
        # 它是 `resolve_backend` 两层判据里更高的一层（之下是服务端开关 `KB_INGEST_BACKEND`）。
        # 在这里取一次、供下面两处调用点共用 —— 一个请求只读一次，免得两次读之间
        # 库上被改、同一份文档的「选链」与「粘性值」对不上。`None` = 跟随开关。
        kb_backend = await self._get_kb_ingest_backend(tenant_id, req.knowledge_base_id)

        # Ensure compiled schema exists for this KB (non-blocking)
        #
        # [jonex] **llm-wiki 库（openkb / jonex_wiki）整段跳过**：它们不用 compiled schema，
        # 而 `auto_compile=True` 有副作用（凭空生成一份 ontology schema）。`reparse_document`
        # 那段早就有同样的守卫与注记，上传这段当初没加 —— 那时 llm-wiki 根本走不到
        # 「进任务中心」这条路，够不到这里。openkb 接进来之后必须补上：否则一次 openkb
        # 上传会顺手给这个库凭空造一份本体 schema（有副作用、且与它的产物口径无关）。
        schema = None
        schema_version = 0
        if kb_type not in LLM_WIKI_TYPES:
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

            # ── [jonex] 接入任务中心的三类库 ───
            # 这一类文档的「解析 → 编译」两步改由任务中心驱动：
            # 这一步只登记接管行 + 提交任务，**下面整段老路径一个字节都不执行**
            # （老路径那一次 `insert(...)` —— 解析（+ lightrag 的推切片）—— 现在由
            # 编排第一步 `parse` 发，档位由运行方按库类型定）。
            #
            # ⚠️ 分叉点只能在这里：它同时满足两个前提 —— ① 所有会失败的校验（重复文件 /
            # 配额 / storage_key 租户段 / COS 对象存在性 / preset 可解析）都已经跑完，
            # 任务中心不会收到一条注定干不成的任务；② 目标 schema 版本已经算出来，
            # 可以在同一处落库（任务中心链的 parse 步要读它做图写 fencing）。
            #
            # 集合只认**运行方那一份事实来源**（`SUPPORTED_KB_TYPES`）—— 在这里再抄一遍
            # 字面量的话，将来接入新库类型时漏改这一处，症状是「新库静默走老链路」。
            # 局部 import 是因为两模块互相依赖（`task_runner_service` 的 handler 反过来
            # 要用 `DocumentService._resolve_parser_preset`），模块级 import 会成环。
            from .task_runner_service import (
                SUPPORTED_KB_TYPES,
                task_source_for_access_type,
            )

            if kb_type in SUPPORTED_KB_TYPES:
                return await self._upload_via_task_center(
                    tenant_id,
                    document_id=doc_id,
                    knowledge_base_id=req.knowledge_base_id,
                    # 任务来源：按这条上传自己的来源方式换算（手动上传 / API 接入 /
                    # API 开放推送 / 文件存储直连）。`data_source_type` 就在本方法上面
                    # 算好了（手动上传那条回落 `file`），**不重新推导一遍**。
                    # 域外取值在这里响亮抛错（落进外层 except → 文档 FAILED），
                    # 与 `backend` 那条配置错的处置同口径。
                    task_source=task_source_for_access_type(data_source_type),
                    # 库类型：与 `backend` 一起决定提交到哪条编排（两类 wiki 库各一条 wiki 链）。
                    kb_type=kb_type,
                    storage_backend=storage_backend,
                    # 两类 wiki 库恒为 0（上面那段按 llm-wiki 跳过了 schema 计算）。
                    schema_version=schema_version,
                    # 走哪条编排：`rag` → 任务中心链（atomic-rag 解析 + 推切片）。
                    # 取值（库级值优先，其次服务端开关）非法会在这里响亮抛错
                    # （落进外层 except → 文档 FAILED，配置错不静默挑一条路走）。
                    backend=ingest_backend.resolve_backend(kb_backend),
                    # 任务名 = 文档名（用户定的）。空白名交给任务中心回落成编排定义名
                    # —— 判据只有那一份，这里不做「是不是空的」判断。
                    task_name=req.file_name,
                    operator_id=user_id,
                )

            # [jonex] llm-wiki KB 级互斥：llm-wiki 文档只解析产出 markdown，不写 LightRAG/Neo4j
            execution_mode = "parse_only" if kb_type in LLM_WIKI_TYPES else "full"

            # [jonex] R1：生成确定性幂等键，insert 响应丢失后重试拿回同一 task_id
            # [jonex] P2: 与对账侧 _build_idempotency_key 同口径，读 content_generation 而不硬编码 :0
            gen = getattr(doc, "content_generation", 0) or 0
            idempotency_key = f"insert:{tenant_id}:{req.knowledge_base_id}:{doc_id}:{gen}"

            # ── [jonex] **提交时**定后端（库级值优先，其次 KB_INGEST_BACKEND）──
            # 取值非法会在这里响亮抛错（落进外层 except → 文档 FAILED，配置错不静默挑路）。
            # 定下来的值随后**写进文档行**，查询侧一律读它 ——
            # 开关/库值翻转后，在途文档继续从它当初提交的后端取状态。
            backend = ingest_backend.resolve_backend(kb_backend)

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
                kb_type=kb_type,                  # [jonex] llm-wiki 产物分支（openkb→content.md / jonex_wiki→content_list.json）
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
            # ── [jonex] **粘性**：把提交时定的后端写进行上 ──
            # 必须在 `set_status(rag_task_id=...)` **之前**、且在**同一次 session** 里：
            # 中途崩溃不许留下「任务建了、却没记住它是从哪个后端起的」的文档 ——
            # 那种行的 rag_task_id 在任何后端都不存在，只能等超时判死。
            # ⚠ SQLAlchemy JSON 列不追踪原地改，必须整体重赋值（与 submit_started_at 同纪律）
            meta = dict(doc.extra_metadata or {})
            meta["ingest_backend"] = backend
            doc.extra_metadata = meta
            status = DocStatus.READY if not rag_result.get("task_id") else DocStatus.PARSING
            await repo.set_status(
                doc,
                status,
                rag_task_id=rag_result.get("task_id"),
                rag_doc_ids=rag_result.get("doc_ids") or rag_result.get("document_ids") or [],
            )
            # [jonex] llm-wiki KB 不抽本体：上传时直接标 ontology_status=READY，
            # 避免对账巡检前 30s 窗口内 ontology_status=PENDING（DB 默认值），
            # 导致前端显示误导的「知识抽取入库中」。
            if kb_type in LLM_WIKI_TYPES:
                # [jonex] 数据运营二期：`emit_task=False` —— 这条路径**没有跑过
                # 知识抽取**（wiki 类库两步只有「文件解析 → 知识编译」），
                # 落一行 extract/success 会让「知识抽取任务数」凭空多出来。
                await repo.set_ontology_status(doc, OntologyStatus.READY, emit_task=False)
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

    # ========================================================
    # [jonex] Ontology 库接入任务中心：上传只负责「登记 + 提交」
    # ========================================================

    async def _upload_via_task_center(
        self,
        tenant_id: str,
        *,
        document_id: str,
        knowledge_base_id: str,
        kb_type: str,
        storage_backend: str,
        backend: str,
        task_source: str,
        schema_version: int = 0,
        task_name: Optional[str] = None,
        operator_id: Optional[str] = None,
    ) -> dict:
        """**接入任务中心的库**（见 `SUPPORTED_KB_TYPES`）的上传：把整条链交给任务中心。

        调用方（`upload_document`）已经按 `kb_type` 分过叉，走到这里的一律要进任务中心；
        `kb_type` 与 `backend` 一起决定提交到哪条编排（见 `definition_for_kb_type`）。

        ── `backend`（入库后端）──

        `backend`（由调用方在分叉点定下，当前恒为 `rag`）决定**提交到哪条编排**：
        `rag` → `definition-file-processing-ontology`（atomic-rag `full` / `reparse_strict`
        解析+推切片，再到 `compile` 步的 `ontology_only`）。链是**两步**。
        这个值**同时**写进文档行的粘性值 —— 运行方与看门狗都靠它决定「去问谁的状态」，
        而且必须在**这一次提交**里落库（见下面「两件事实同一次提交」）。

        ── 顺序是刻意定死的（先落库、后提交）──

        1. **文档行已经落好了**（调用方在同一方法更早处 INSERT，`status=PARSING`），
           而且所有会失败的校验都在那之前跑完（重复文件、配额、`storage_key` 租户段、
           COS 对象存在性、preset 可解析）。这里只做「登记接管行 + 提交任务」，
           **不再做任何业务校验** —— 反过来的顺序会把常见的失败变成有害的：任务中心的
           后台循环恒开，`init_task` 一返回任务就是 `pending`，下一轮立刻派活；那时
           文档行还不存在的话，handler 只能显式拒，**子任务直接终态 `failed`**，
           留下一份删不掉的死任务（`tasks.id` 是全局主键、终身占用）。
        2. **接管行先落库、再 `submit`**。反过来的话，「任务建成了、接管行没写」这种崩法
           会留下一条我们**认不出来**的任务（`remove_task` 按 `task_id` 反查接管行，
           查不到就什么也清不掉）。
        3. **`submit` 失败不抛**。抛出去会被调用方的 `except` 把文档置 `FAILED` ——
           而这一步的失败绝大多数是**瞬时**的（任务中心还没起来、Sidecar 正在重启）。
           接管行留在 `submitting`，看门狗每 30 秒拿**同一个 `task_id`** 补交，直到
           30 分钟窗口用完才判死。这条补偿就是「不做 submission outbox」的替代品。

        ⚠️ **第 3 条的代价写在明处**：这份文档的失败最坏要等 30 分钟才可见。这是有意的
        —— 拿「立刻可见」换掉「一次瞬时抖动 = 一份作废的上传」（`submit` 自己还带
        3 次重试，真正落到这里的是「重试也没够着」）。

        ── 任务名（`task_name`）──

        任务实例名，调用方传**文档名**（用户定的）。它**只影响展示**（任务列表里那一列），
        不进 `inputs`、不决定跑哪条编排、不参与任何判定 —— 所以传空是合法的（任务中心
        会回落成编排定义名）。⚠️ 它在**任务诞生那一刻**定死：「重新解析 / 重新编译」走的是
        「重跑某几步」、任务行不重建，名字不会跟着变。

        ── 任务来源（`task_source`，必传，2026-10-13 加）──

        这条任务是哪个入口来的（`knowledge_base.file.manual_upload` / `.api_pull` /
        `.api_push` / `.storage`）。**由调用方在分叉点按本方法的 `data_source_type`
        换算好传进来**（`task_source_for_access_type`），这里不重推 —— 同一条映射
        两个调用点共用，才不会分叉。看门狗补交时按同一个值重算。

        ⚠️ 它同样**只在任务诞生那一刻**有意义，且不参与任何判定（同 `task_name`）。

        ── 为什么 `space_id` 要回查知识库行 ──

        `init_task` 会校验空间归属，而**看门狗补交那一帧没有请求上下文**、`TenantContext`
        是空的。所以 `space_id` 必须落进接管行，那时唯一的来源就是这里查出来的这个值
        （接管行模型的 `space_id` 注释写了同一条）。

        拿不到空间是**数据/配置问题**（知识库行缺失、`space_id` 为空），重试换不来一个
        空间 —— 所以这里**抛**，由调用方的 `except` 把文档置 `FAILED`，并且**不留下接管行**
        （没有任何东西需要看门狗补交）。
        """
        # 局部 import：`task_runner_service` 反过来要在 handler 里用 `DocumentService`
        # （解析 preset 那一处），模块级互相 import 会成环。
        from .task_runner_service import submission_accepted, submit_ontology_task

        # 任务 id 由**我们**生成：它是这条路唯一的重试前提（`submit` 只在自带 `task_id`
        # 时重试），生成完立刻落到接管行上 —— 不等回包才知道写什么。
        task_id = str(uuid4())

        async with get_db_session() as session:
            kb = await KnowledgeInfoRepository(session).get_by_id(knowledge_base_id, tenant_id)
            space_id = (getattr(kb, "space_id", "") or "") if kb is not None else ""
            if not space_id:
                raise InvalidParameterError(
                    message=translate(
                        "err.kb.task_center.space_missing",
                        params={"knowledge_base_id": knowledge_base_id},
                        fallback=f"知识库 {knowledge_base_id} 没有可用空间，无法提交任务中心任务",
                    ),
                    details={"knowledge_base_id": knowledge_base_id},
                )

            await TaskCenterDocumentRepository(session).create(
                TaskCenterDocument(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    space_id=space_id,
                    task_id=task_id,
                    operator_id=operator_id,
                    state=TaskCenterDocState.SUBMITTING.value,
                )
            )
            # ── 两件事实与接管行**同一次提交** ──
            # 模型文件的模块 docstring 钉了同一条纪律（接管行与文档状态的写入必须在
            # 同一个事务里）：分两次提交的话，中间崩一下就留下「任务已经被接管、
            # 文档行却还看不出走的是哪条链」的残局 —— 而看门狗补交与 handler 都要读
            # 这个值，读到空就只能按默认链走（文档走错链，且没有任何报错）。
            #
            #   ① 目标 schema 版本（P1-E 图写 fencing）：链的 `parse` 步从这列读它、
            #      随 `insert` 传到 atomic-rag。与老路径同口径。
            #   ② 走哪条编排的粘性值：运行方与看门狗都靠它分流（`backend_for_document`）。
            #      ⚠ **整块替换** `extra_metadata`（SQLAlchemy 不追踪 JSONB 的就地修改，
            #      老路径 `meta["ingest_backend"] = backend` 是同一条写法的另一处）。
            #   ③ 这次是哪种操作的粘性值（`task_intent`）：此刻恒为 `upload` —— 写它是
            #      为了让读取侧的回落（缺失 → `upload`）永远不成为承重结构：一份**重新
            #      解析过**的文档也会带着 `reparse` 躺在那儿，靠回落去区分是不可能的。
            #   ④ 库类型：运行方分派（`_prepare_step` 拿它进快照）、看门狗（`compile`
            #      步按它决定看哪一列终态）、既有对账都读它。老路径在 `doc.extra_metadata`
            #      里写的就是这个键（`…, "kb_type": kb_type}`），链条这条也照写 ——
            #      否则每个读点都要回落成「再查一次 knowledge_info」。
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_by_id(document_id, tenant_id)
            if doc is not None:
                if schema_version:
                    doc.ontology_target_schema_version = schema_version
                doc.extra_metadata = task_intent.with_intent(
                    {
                        **(doc.extra_metadata or {}),
                        "ingest_backend": backend,
                        "kb_type": kb_type,
                    },
                    task_intent.INTENT_UPLOAD,
                )
                # [jonex] **Wiki 类库不抽本体，任务中心这条路也要把它标成 READY。**
                #
                # 与老路径（本文件 `if kb_type in LLM_WIKI_TYPES: await repo.set_ontology_status(
                # doc, OntologyStatus.READY)`）**同一个理由**，这里从前漏了 —— 代价是实测
                # 7/7 份 openkb 文档全部停在 `ontology_status=failed`：
                #
                # 不标的话这一列会一直是 DB 默认的 `PENDING`，而看门狗在 `parse` 步收口时
                # **只写 `status=READY`、一个字节都不碰这一列** → 于是产生
                # 「**已 READY、但 ontology 仍 PENDING**」的窗口。本体对账的领取条件正好是
                # 这个组合（`list_by_ontology_status_for_reconciliation`），而 `_retry_ontology_one`
                # 的 docstring 明写它**没有** kb_type 守卫、理由是「完成时 `set_status(READY)`
                # 与 `set_ontology_status(READY)` 在同一次 commit 里，没有这个窗口」——
                # **那条假设对任务中心这条路不成立**（两列分别由看门狗与本体链写，
                # 从来不在同一次 commit 里）。
                #
                # 后果链：对账给一个**不抽本体的 Wiki 库**去抽本体 → `get_compiled_schema(
                # auto_compile=True)` 凭空造一份 schema（正是本文件上面那段拼命避免的副作用）
                # → 投一个本体任务 → 最终落到 `FAILED`，前端显示误导性的「知识抽取失败」。
                if kb_type in LLM_WIKI_TYPES:
                    await repo.set_ontology_status(doc, OntologyStatus.READY)

        # ── 提交（自带 task_id；失败只留痕、不抛，见 docstring 第 3 条）──
        try:
            result = await submit_ontology_task(
                tenant_id=tenant_id,
                space_id=space_id,
                task_id=task_id,
                document_id=document_id,
                knowledge_base_id=knowledge_base_id,
                # 走哪条编排：`kb_type` + `backend` 两件一起定（openkb 只有一条链；
                # lightrag 由开关的粘性值分两条）。`backend` 用**这次提交时定下的粘性值**，
                # **不要**重读开关（开关翻转过的话，这里重读会让「文档行粘性值」与
                # 「提交到的定义」不一致 —— 而看门狗补交、handler 分流读的都是那个粘性值）。
                kb_type=kb_type,
                backend=backend,
                # 任务来源（任务中心那条路必传）：调用方按文档的来源方式换算好传进来。
                # 它落 `tasks.task_source`，是「这条任务是哪个入口来的」的唯一答案。
                task_source=task_source,
                task_name=task_name,
                operator_id=operator_id,
            )
        except Exception as exc:  # noqa: BLE001
            # 连裁决都没拿到（装配错、租户收不出、报文非法）。留痕 + 计数，看门狗补交。
            logger.warning(
                "任务中心提交异常（看门狗将补交）: doc=%s task=%s err=%s",
                document_id, task_id, exc,
            )
            await self._record_submit_failure(document_id, f"提交任务异常：{exc}")
            return await self._current_doc_dict(document_id, tenant_id)

        if submission_accepted(result):
            async with get_db_session() as session:
                await TaskCenterDocumentRepository(session).mark_managed(
                    document_id, tenant_id=tenant_id, task_id=task_id,
                )
            logger.info(
                "任务中心接管已提交: doc=%s kb=%s task=%s disposition=%s",
                document_id, knowledge_base_id, task_id, result.disposition.value,
            )
        else:
            logger.warning(
                "任务中心未受理（看门狗将补交）: doc=%s task=%s disposition=%s message=%s",
                document_id, task_id, result.disposition.value, result.message,
            )
            await self._record_submit_failure(
                document_id, result.message or f"任务中心未受理（{result.disposition.value}）"
            )

        return await self._current_doc_dict(document_id, tenant_id)

    async def _record_submit_failure(self, document_id: str, reason: str) -> None:
        """记一次提交失败（`submit_attempts += 1` + 留痕）。

        **只写接管行、不碰文档状态** —— 文档仍是 `PARSING`，这是「还没失败」的诚实表达：
        看门狗还在补交，此刻判 `FAILED` 会把一次可自愈的提交抖动写成终局。
        """
        async with get_db_session() as session:
            await TaskCenterDocumentRepository(session).bump_submit_attempt(
                document_id, reason,
            )

    @staticmethod
    async def _current_doc_dict(document_id: str, tenant_id: str) -> dict:
        """回读文档行的对外快照 —— 上传接口的返回体形状与老路径保持一致。"""
        async with get_db_session() as session:
            doc = await KnowledgeDocumentRepository(session).get_by_id(document_id, tenant_id)
            return doc.to_dict() if doc is not None else {"id": document_id, "tenant_id": tenant_id}

    async def generate_upload_url(
        self, tenant_id: str, kb_id: str, file_name: str, content_type: str | None = None,
        file_size: int | None = None,
    ) -> dict:
        """生成 COS 预签名 PUT URL 和 storage_key（D9）。

        前端/网关直传字节到 COS（不经 Sidecar 透传），
        然后再调 upload_document 传 storage_key 确认。

        file_size：客户端声明的待上传字节数。非空时先做上限校验，再把 Content-Length
        头纳入签名，锁死客户端必须上传该精确字节数（防随意扩大）；local 后端返回空
        upload_url 降级走 multipart 上传。
        """
        from uuid import uuid4

        tenant_id = require_tenant(tenant_id)
        if file_size is not None and (file_size <= 0 or file_size > _MAX_UPLOAD_SIZE):
            raise InvalidParameterError(
                message=translate(
                    "err.upload.size_exceeded",
                    params={"size": file_size, "max": _MAX_UPLOAD_SIZE},
                    fallback=f"文件大小超限：{file_size} 字节，最大允许 {_MAX_UPLOAD_SIZE} 字节",
                ),
                details={"file_size": file_size, "max_size": _MAX_UPLOAD_SIZE},
            )
        doc_id = str(uuid4())
        storage_key = build_object_key(tenant_id, kb_id, doc_id, file_name)

        storage = get_object_storage()
        # local 后端显式实现 presigned_put_url 返回空串降级，cos 后端按 content_length 签名
        upload_url = await storage.presigned_put_url(
            storage_key, tenant_id=tenant_id, expires=300, content_length=file_size,
        )

        return {
            "doc_id": doc_id,
            "storage_key": storage_key,
            "upload_url": upload_url,
            "storage_backend": os.getenv("OBJECT_STORAGE_BACKEND", "local"),
        }

    async def get_raw_location(self, tenant_id: str, knowledge_base_id: str = "", document_id: str = "",
                               download: bool = False) -> dict:
        """获取文档原文位置信息（校验租户归属后返回）。

        统一 raw 入口：
        - 对象存储后端（cos/s3）：返回 presigned_url，gateway 302 直跳（天然支持 Range/流式）；
        - local 后端：presigned_url 为空，返回 storage_key，gateway 用 FileResponse
          从共享卷流式返回（支持 Range，音视频可拖动/边下边播，不经 Sidecar 传字节）。

        download=True 时对象存储预签名 URL 附带
        response-content-disposition: attachment; filename*=UTF-8''<原名>，
        浏览器下载并使用原始文件名（local 后端的 attachment 由 gateway 处理）。

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
        # [jonex] office 查看原文定位：office 文档返回解析侧持久化的 preview.pdf，
        # 供前端 iframe + #page=N 定位（download=True 仍返回原始 office 文件）。
        # preview 与图片资产同源——上传（OfficePreviewUploadStage）走全局后端
        # get_object_storage()，查看侧必须同源，否则「后端从 local 切 cos 后
        # reparse」会出现 preview 实际在全局后端、查看却按 doc 后端找的错配
        # （与 get_asset_raw_location 的口径一致）。
        if classify_media(doc.mime_type, doc.file_name) == "office" and not download:
            preview_key = build_preview_key(tenant_id, doc.knowledge_base_id, doc.id)
            preview_backend = os.getenv("OBJECT_STORAGE_BACKEND", "local").strip().lower()
            preview_storage = get_object_storage()
            if await self._office_preview_available(preview_storage, preview_key):
                presigned = await preview_storage.presigned_url(
                    preview_key, tenant_id, expires=300,
                ) or ""
                return {
                    "storage_backend": preview_backend,
                    "storage_key": preview_key,
                    "mime_type": "application/pdf",
                    "file_name": doc.file_name,
                    "presigned_url": presigned,
                }
            # 存量 office 文档无 preview（入库早于本方案）→ 回退原始文件下载
            logger.warning(
                "office 文档 preview.pdf 缺失，回退原始文件: doc=%s", doc.id,
            )
        presigned = ""
        if backend in ("cos", "s3"):
            disposition = None
            if download:
                disposition = "attachment"
                if doc.file_name:
                    disposition += f"; filename*=UTF-8''{quote(doc.file_name)}"
            presigned = await get_object_storage_for(backend).presigned_url(
                doc.storage_key, tenant_id, expires=300, disposition=disposition,
            )
        return {
            "storage_backend": backend,
            "storage_key": doc.storage_key,
            "mime_type": doc.mime_type or "application/octet-stream",
            "file_name": doc.file_name,
            "presigned_url": presigned or "",
        }

    async def _office_preview_available(self, storage, key: str) -> bool:
        """[jonex] office 查看原文定位：检测 preview.pdf 是否已上传。

        COS/S3 的 presigned_url 是纯本地签名，不检查对象存在性，故须主动检测：
        - local：fs_path 返回绝对路径，用 os.path.exists；
        - cos：head_object；
        - s3：无 head 能力，退化为尝试 get_bytes（对象不存在抛异常 → False）。
        best-effort：检测异常一律按不存在处理，回退原始文件下载。
        """
        try:
            fs_path = storage.fs_path(key) if hasattr(storage, "fs_path") else None
            if fs_path:  # local 后端返回绝对路径
                return os.path.exists(fs_path)
            if hasattr(storage, "head_object"):  # cos
                return bool(await storage.head_object(key))
            await storage.get_bytes(key)  # s3 兜底
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("office preview 存在性检测失败（视为缺失）: %s", exc)
            return False

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

        # Jonex Wiki reads saved compile inputs; OpenKB keeps its shared Markdown reader.
        kb_type = await self._get_kb_type(tenant_id, doc.knowledge_base_id)
        if kb_type in LLM_WIKI_TYPES:
            return await self._get_document_wiki_chunks(tenant_id, doc, kb_type)

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

    def _get_llm_wiki_chunks(self, document_id: str, kb_type: str) -> dict:
        """OpenKB 的兼容 Markdown 视图，保持既有共享卷读取和分块规则。

        Jonex Wiki 经 _get_document_wiki_chunks 读取真实编译输入，不走此展示分块。
        """
        from .llm_wiki_artifacts import load_llm_wiki_blocks

        blocks, _artifact = load_llm_wiki_blocks(kb_type, document_id)
        if not blocks:
            return {"doc_id": document_id, "total": 0, "chunks": [], "kb_type": kb_type}

        chunks = [
            {
                "chunk_id": f"{document_id}#{kb_type}-{i}",
                "content": b,
                # 与 LightRAG chunk 结构对齐：前端解析结果页依赖 content_summary/file_path
                # （getChunkTitle 会读取二者），标签行依赖 content_length。llm-wiki 无
                # file_source 锚点/非 LightRAG chunk，file_path 置空串、tokens 等置 None
                # 而非缺省，避免前端 `.match()` 到 undefined。
                "content_summary": b,
                "content_length": len(b),
                "tokens": None,
                "chunk_order_index": i,
                "full_doc_id": None,
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
            "kb_type": kb_type,
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

    async def get_chunk(self, tenant_id: str, document_id: str, chunk_id: str) -> dict:
        """按 chunk_id 精确直查单片内容（直连 LightRAG text_chunks，不拉整篇列表）。

        校验 doc 租户归属后，按 chunk_id 直查；再用 chunk 的 file_path `doc=` 锚点校验其确实
        归属于 document_id（防跨文档串取）。未命中抛 ResourceNotFoundError。只认 chunk_id。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)  # 租户+归属校验

        # Wiki single-chunk lookup uses the same source as the list view.
        kb_type = await self._get_kb_type(tenant_id, doc.knowledge_base_id)
        if kb_type in LLM_WIKI_TYPES:
            listing = await self._get_document_wiki_chunks(tenant_id, doc, kb_type)
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
        allow_pending: bool = False,
        reason: Optional[str] = None,
    ) -> dict:
        """按文档 id 重新解析。

        复用文档已存的 storage_key / storage_backend / 所属 KB，重新走"按文件类型选 preset
        + 推送 compiled ontology schema"链路，经 atomic-rag `retry` action 强制重解析。
        与上传一致：preset 解析不到 → 文档标记解析失败、保留记录、不推送。

        [jonex] R1-c：默认跳过源文件未变化的 reparse（比对内容 md5）。
        传 force=True 可绕过 hash 检查、强制重跑（适应解析配置变更、本体规则调整等场景）。

        `allow_pending=True` 仅供**恢复归档**链路：它先把 `archived` 条件迁移成
        `pending`（留一条 `archived -> pending` 事件），再调本方法。互斥闸的
        本意是「别和正在跑的解析撞车」，`PENDING` 若由调用方在同一事务前显式
        置入、且没有在途任务号，就是安全的。`PARSING / INGESTING` **始终拒绝** ——
        那两个状态意味着下游真的在跑。

        `reason` 写进本次 `-> parsing` 的 `asset_status_event`：按钮传
        `reparse`、任务重试传 `task_retry`、归档恢复传 `manual_restore`。
        """
        tenant_id = require_tenant(tenant_id)

        # ── [jonex] 2026-09-30：**先确认源对象还在，再动任何库里的东西** ──
        #
        # 这道检查从前排在整段写入**之后**（武装接管行 → 提交 → 再查对象存储），于是
        # 「源对象已经不在」（生命周期清理 / 桶或签名配错）这条路上会留下一张**没人回收的
        # `managed` 卡**：程序回写 FAILED 就报错走了，可接管行还停在在途态 →
        # `exists_active` 恒真 → **重新解析 / 重新抽取 / 整库重置三条路全被 1003 拒**，
        # 最长到 `TC_NO_DISPATCH_SEC`（默认 30 分钟）才自愈，而**任务中心那边压根没有
        # 任务在跑**（用户看到的是「报错之后这份文档什么都干不了，半小时后自己好了」）。
        #
        # 挪到最前面之后：检查不过就当场报错、**库里的接管行一个字都没动**，不存在要回收
        # 的东西。两处顺带的变化，都是修：
        #   · 从前那次「先回写 FAILED」现在**不需要了** —— 文档还没被置成 PARSING、不会
        #     卡住；而且用户点的是「重新解析」、不是「这份文档坏了」，把一份**本来可用的**
        #     文档改成 FAILED 反而是破坏（源对象没了这件事由错误消息讲清楚）；
        #   · 从前那句 SUCCESS 审计排在检查之前，等于给一次**没做成的**操作记了成功 ——
        #     现在检查不过就不会走到审计。
        #
        # 代价（有意接受）：这一趟多一次单行读，只为在开主事务之前拿到
        # `storage_key` / `storage_backend`。
        async with get_db_session() as session:
            probe = await KnowledgeDocumentRepository(session).get_required(
                document_id, tenant_id
            )
            probe_key = probe.storage_key
            probe_backend = (probe.storage_backend or "local").strip().lower()
        if probe_backend in ("cos", "s3"):
            try:
                exists = await get_object_storage().head_object(probe_key)
            except Exception as exc:  # noqa: BLE001
                # ⚠️ **「查不了」≠「不在」，一律放行**（与同一个方法里 `_compute_source_hash`
                # 的 fail-open 同一条口径）：拦在这里，等于把一次**可能只是查不了**的重解析
                # 直接做不到。两种成因：
                #   · **本进程的存储后端没有 `head_object`** —— `LocalObjectStorage` 就没有；
                #     「行上写着 `cos`、而 `OBJECT_STORAGE_BACKEND=local`」时必撞（老行 +
                #     换过后端），这在从前会让整个 reparse 冒成 500「执行失败」；
                #   · 存储侧抖了一下（超时 / 凭据过期）。
                # 真要命的是**源对象确实不在**那一种 —— 它回来的是 `False`，照样拦住。
                logger.warning(
                    "reparse 前置的源对象探查没做成（按「查不了」放行）doc=%s key=%s: %s",
                    document_id, probe_key, exc,
                )
                exists = True
            if not exists:
                raise ResourceNotFoundError(
                    message=translate(
                        "err.cos.object_deleted",
                        params={"storage_key": probe_key},
                        fallback=f"COS 对象不存在或已被删除: {probe_key}",
                    ),
                    details={"storage_key": probe_key},
                )

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
            # [jonex] P0-I 互斥：正在解析/入库中（PARSING/INGESTING）拒绝重复 reparse，
            # 避免两个 reparse 并发用不同 old_ids 快照互删、旧代次覆盖新代次。
            # `PENDING` 默认同样拒绝（排队待处理 = 已有人接手），只有恢复归档链路
            # 用 `allow_pending=True` 显式放行 —— 那个 PENDING 是它在**同一请求里**
            # 刚由 `archived` 条件迁移过来的、没有任何在途任务号。
            _status = doc.status.value if hasattr(doc.status, "value") else doc.status
            if _status in (DocStatus.PARSING.value, DocStatus.INGESTING.value) or (
                _status == DocStatus.PENDING.value and not allow_pending
            ):
                raise ResourceConflictError(message=translate("err.doc.parsing_or_cleaning", fallback="文档正在解析/入库中，请等待完成后再重新解析")  )  # 原消息)
            if doc.status == DocStatus.DELETING.value:
                raise ResourceConflictError(message=translate("err.doc.deleting_cannot_reparse", fallback="文档正在删除中，无法重新解析")  )  # 原消息)
            # ── [jonex] 任务中心接管中：**拦住用户、让他等**（§3.7）──
            # 判据取**接管行**而不是文档状态：Ontology 库的文档在 `parsed` / `ready` 这些
            # 「看着能操作」的稳定态上也可能正被链子推着（等下一步派活 / 等本体编译），
            # 光看 `doc.status` 拦不住。放行的唯一出口是看门狗把接管行置 `released`
            # （收口成功 / 判死）—— 所以「看到这句话」= 当前步骤还没结束。
            # ⚠️ 与下面那条 in-flight 检查（查 `rag_task_id`）不是一回事：那条看的是
            # **下游任务号**，本条在 `submitting` 阶段（还没派活、没有任务号）就得挡住。
            if await TaskCenterDocumentRepository(session).exists_active(document_id, tenant_id):
                raise ResourceConflictError(
                    message=translate(
                        "err.doc.task_center_managed",
                        fallback="该文档正在由任务中心处理，请等当前步骤结束后再重新解析",
                    )
                )
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
            is_llm_wiki = kb_type in LLM_WIKI_TYPES

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
            if can_skip and is_llm_wiki:
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

            # [jonex] **回滚快照**：下面这一整段改写（代次 +1 / `PARSING` / 清
            # `rag_task_id` / 写 `tc_intent=reparse` …）与**接管行的武装是同一次提交**
            # （刻意的，见下面 `tc_plan` 那段），所以它在「任务中心受理」之前就已经落库了。
            # 任务中心**拒收**时（见下面 `retry_accepted` 那一段）光交还接管行是不够的，
            # 得把这里记下的旧值原样写回去，否则一份本来 READY 的好文档会停在没有任务号的
            # `parsing` 上、随后被既有对账按「RAG 任务确认丢失」连判 3 拍 → FAILED。
            # ⚠️ **只在本方法这一条路上用**（那次提交之后、受理之前没有别的写者能进来：
            # 接管行已在途，看门狗那几个入口都被「正在由任务中心处理」挡住）。
            # 状态列 + `extra_metadata` 里那几项「这次重来」的键**整段搬进了
            # `reset_document_for_rerun`** —— 因为任务中心的「任务重试」也要做同一件事
            # （它撞上一张已交还的接管行时，就地按同一口径重置文档行），
            # 两处各写一份迟早会漂移，而漂移的后果是**静默的假成功**（见那个函数的 docstring）。
            reparse_rollback = reset_document_for_rerun(doc, is_llm_wiki=is_llm_wiki)
            new_generation = doc.content_generation
            # [jonex] 第 3 期：这次 `-> parsing` 的流转留痕。`reset_document_for_rerun`
            # 是直写属性（不走 `set_status`），所以事件得在这里补 —— 与任务重试那条
            # 路（`task_runner_service`）同一口径，用同一个 rollback 快照取改前状态。
            await safe_record_asset_status_event(
                session, tenant_id,
                asset_id=document_id,
                status=doc.status,
                previous_status=reparse_rollback.get("status"),
                reason=reason or "reparse",
                kb_id=doc.knowledge_base_id,
            )

            # [jonex] R1-c：持久化源文件 hash + 配置指纹到 extra_metadata，供后续 reparse 比对
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
            if is_llm_wiki:
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
            # ── [jonex] reparse 写回后端粘性值（下面走的是 `get_rag_client().retry`）──
            # 这次重新解析由 atomic-rag 承接，行上的**粘性值按同一口径写回 `rag`**，
            # 保证查询侧「这份任务是谁建的、状态就问谁」的判据始终成立。
            extra["ingest_backend"] = "rag"

            # ── [jonex] 三类库：这次重新解析**改由任务中心重跑**（§4.2 ④）──
            # 用户定的两条：① 只针对接了任务中心的库（`lightrag` 与 `openkb` 自 2026-09-29
            # 起，`jonex_wiki` 自本轮起）；② **不是新起一个任务** —— 所以这里调的是任务中心
            # 现成的批量口 `retry_subtasks`，把这条任务里的「解析」与「编译」两步原地重跑
            # 一次。发起在下面的 `tc_plan` 分支里，那一步必须等本次提交落库之后才做
            # （接管行得先是被重跑的状态）。
            #
            # ⚠️ 判据**只认「有没有接管行」**，不认按钮名之外的任何东西：
            #   · 没有接管行 → 明确报错（老文档 / 任务被删过），**绝不偷偷新建一条任务**；
            #   · 接管行在途 → 上面那道 `exists_active` 已经拦过（用户被要求「等」）。
            #   · 两类 wiki 库**没有**「严格重解析」那一档（它们不推切片），它们的 `parse`
            #     步照发 `parse_only` —— 分支条件因此不比 lightrag 更严，只是都走这条。
            # 同样只认运行方那一份事实来源（`SUPPORTED_KB_TYPES`），不另抄字面量。
            # 局部 import：`task_runner_service` 反过来要在 handler 里用 `DocumentService`
            # （解析 preset 那一处），模块级互相 import 会成环。
            from .task_runner_service import (
                SUPPORTED_KB_TYPES,
                rearm_for_rerun,
                reparse_step_codes,
            )

            tc_plan: Optional[Dict[str, str]] = None
            # 这次交出去的**名单**（任务中心把「顺序」当语义用，见 `retry_subtasks_for_steps`）。
            # 与 `tc_plan` 在同一处赋值 —— 下面那段用它，别再从这里猜节点名。
            tc_step_codes: Optional[Tuple[str, ...]] = None
            if kb_type in SUPPORTED_KB_TYPES:
                tc_plan = await rearm_for_rerun(
                    session,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    step_code="parse",
                )
                # ── [docq] 名单只放**已终态的节点**（2026-10-14，真机复现后修）──
                # 从前这里恒交 `(parse, compile)` 两步，而任务中心对名单里**每一行**都要求
                # 「那一步已终态」⇒ 从未编译过的文档（`compile_1` 必然是 `pending`，那正是
                # 「解析失败后重试」要覆盖的那一档）**整批被拒**、主用例
                # 「解析失败后重试」100% 不可用。判据与逐条论证收在 `reparse_step_codes`。
                #
                # ⚠️ **必须在本次提交里读**（就在武装接管行之后、提交之前）：读回来是
                # `running` 时那个异常会把整笔写入（代次 +1 / `PARSING` / 清 `rag_task_id` /
                # 接管行的武装）一起回滚 —— 拒绝得干净，不会留下「库里的行说重跑解析、
                # 而任务中心那一步压根没被交出去」的半成品。这一段**不能挪到下面 `tc_plan`
                # 那个分支里**（那里已经提交过了，报错就得自己回滚一遍）。
                #
                # ⚠️ **[docq] `running` 那条拒绝必须在这里留痕**（2026-10-14 上一轮审查
                # Minor 1 收口）：这条拒绝在「名单只放已终态节点」之前由任务中心那一侧发出
                # （整批被拒 → 走下面「未受理」那段，那里有审计 + warning）；现在它提前到
                # 提交之前、**不再经过那段**了 —— 不在这里补一笔，用户「点了重新解析没反应」
                # 时审计里什么都查不到，而同一方法里另一条拒绝路径（下面未受理）也刻意
                # 留了痕。形状照抄未受理那条
                # （`action="reparse_document"` + `outcome="FAILED"`），不自创字段。
                # `schedule_emit` 是 fire-and-forget：即便随后整笔回滚（**它确实会回滚**），
                # 审计行仍在 —— 用户确实点了、确实被拒了，这正是要记的。
                try:
                    tc_step_codes = await reparse_step_codes(
                        tenant_id=tenant_id,
                        task_id=tc_plan["task_id"],
                        document_id=document_id,
                    )
                except ResourceConflictError as exc:
                    schedule_emit({
                        "tenant_id": tenant_id,
                        "user_id": _audit_user_id(user_id),
                        "username": username,
                        "ip": ip,
                        "log_type": "TASK",
                        "action": "reparse_document",
                        "outcome": "FAILED",
                        "service_name": "knowledge_base",
                        "resource": ResourceType.DOCUMENT.value,
                        "resource_id": str(document_id),
                        "error_message": str(exc)[:1000],
                        "request_params": {
                            "reason": "compile_step_running",
                        },
                    })
                    raise
                # 与接管行的武装**同一次提交**：分两次的话，中间崩一下就留下
                # 「接管行说重跑解析、文档行还说这是上传的解析」—— 而运行方按后者
                # 分派，会把一次「重新解析」干成「白解析一遍」（切片没重推、旧切片没删，
                # 用户看到的是「点了没反应」）。
                extra = task_intent.with_intent(extra, task_intent.INTENT_REPARSE)
            doc.extra_metadata = extra
            # [jonex] 数据运营二期：reparse 的 parse/running 埋点。
            # ⚠️ 必须带 **new_generation** —— `build_processing_task_id` 的键含代次，
            # 用 0（文档初值）会与终态行（用的是行上的新代次）算出**不同的键**，
            # 那行 running 就永远收不掉（僵尸，`pipeline_task_stats.running` 恒高）。
            # 不走 `set_status`：本行是直写属性、不经它，且这里拿不到「改前」的旧值。
            await record_processing_task(
                session, tenant_id,
                asset_id=document_id, task_type=TASK_TYPE_PARSE,
                status=TASK_STATUS_RUNNING, doc_id=document_id,
                kb_id=doc.knowledge_base_id, content_generation=new_generation,
            )
            await session.flush()

            doc_dict = doc.to_dict()

        uid = _audit_user_id(user_id)
        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": uid,
            "username": username,
            "ip": ip,
            "log_type": "TASK",
            "action": "reparse_document",
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(document_id),
        })

        # 对象存储后端（cos/s3）的「源对象还在不在」这道检查**已挪到本方法最前面**
        # （2026-09-30）：从前它排在这里 = 排在武装接管行与提交**之后**，检查不过就会
        # 留下一张没人回收的 `managed` 卡，把这份文档的三个入口一起锁到 30 分钟兜底。
        # **别再在这里加回来** —— 那正是要治的那个形状。

        # ── [jonex] Ontology 库：把名单（解析，外加已跑过时的编译）**一次**交给任务中心重跑 ──
        # 走到这里说明接管行已经武装好（`rearm_for_rerun`，与上面那些写入同一次提交）。
        # 这一步**不再自己调引擎**：任务中心会把 `parse_1` 派回来，由 handler 按文档行上的
        # `tc_intent` 分派 —— `reparse` 走「严格重解析」；它成功之后再派 `compile_1`
        # 抽本体（先后由任务中心写进依赖，见 `retry_subtasks_for_steps`）。
        if tc_plan is not None:
            from .task_runner_service import (
                node_id_for_step,
                retry_accepted,
                retry_subtasks_for_steps,
            )

            if tc_step_codes is None:  # pragma: no cover
                # 走不到：两者在同一处赋值（上面 `rearm_for_rerun` 那一段）。留这一道是因为
                # 「名单为空 / 拿不到」如果被静默咽掉，用户看到的是「点了重新解析但什么都没
                # 发生」—— 那比一个指不清的报错难查得多。
                raise InternalError(message="重新解析的步骤名单没有算出来（tc_step_codes 为空）")

            retry_result = await retry_subtasks_for_steps(
                tenant_id=tenant_id,
                space_id=tc_plan["space_id"],
                task_id=tc_plan["task_id"],
                # ⚠️ **顺序就是执行顺序**（第 i 步等第 i-1 步成功）：先解析（重建切片）、
                # 成功后再编译（按新切片抽本体）。写反了任务中心会**明确拒绝**（连成环），
                # 不会静默跑错 —— 这正是「顺序有意义」的证明。
                # ⚠️ 名单由 `reparse_step_codes` 按「那一步跑到哪了」定（只放已终态的节点），
                # **不在这里写死两步** —— 理由见那里与 `tc_step_codes` 的注释。
                step_codes=tc_step_codes,
                operator_id=user_id,
            )
            if not retry_accepted(retry_result):
                err_msg = translate(
                    "err.kb.task_center.reparse_not_accepted",
                    params={"reason": retry_result.message or ""},
                    fallback=f"任务中心未受理这次重新解析：{retry_result.message}",
                )
                # 没被受理 → **交还接管行 + 把文档行滚回去 + 显式报错**。
                # 交还是必须的：留着那条在途行，用户会被「正在由任务中心处理」那道闸
                # 一直挡住（文档还是 `parsing`，`patrol` 也救不了他），而看门狗要等满
                # `TC_NO_DISPATCH_SEC`（默认 30 分钟）才放行。
                # 报错也是必须的：用户点的是「重新解析」，不能显示成成功。
                #
                # ⚠️ **回滚文档行同样必须（2026-10-09 补）**：这次改写与接管行的武装是
                # **同一次提交**、早就落库了。不回滚的话，一份本来 READY 的好文档会停在
                # 「`parsing` 且没有 `rag_task_id`」上 —— 接管行已交还、这一行回到既有对账
                # 手里，对账按「没有任务号」走 `_verify_chain(entry="null_task_id")`，
                # 宽限过后若 storage 兜底也找不回旧切片，就连判 3 拍 → FAILED
                # 「RAG 任务确认丢失…请重新上传」。用户看到的是「操作被拒」，
                # 代价却是一份可用文档被标坏（重上传一遍）。
                # 快照在**上面那次提交之前**取（`reparse_rollback`），这里按它写回。
                async with get_db_session() as session:
                    await TaskCenterDocumentRepository(session).mark_released(
                        document_id,
                        error_message=(
                            f"任务中心未受理重新解析：{retry_result.message}"
                        )[:2000],
                    )
                    rolled = await KnowledgeDocumentRepository(session).get_by_id(
                        document_id, tenant_id
                    )
                    if rolled is not None:
                        for _col, _old in reparse_rollback.items():
                            setattr(rolled, _col, _old)
                        # 埋点行一起收口：上面那次 `record_processing_task` 是按**新代次**
                        # 落的一行 `running`，而本该收口它的解析管线不会再跑（任务中心
                        # 没受理这一步）—— 不收它，数据运营的 KPI 上就留一个永久 running。
                        await record_processing_task(
                            session,
                            tenant_id,
                            asset_id=document_id,
                            task_type=TASK_TYPE_PARSE,
                            status=TASK_STATUS_FAILED,
                            doc_id=document_id,
                            kb_id=rolled.knowledge_base_id,
                            content_generation=new_generation,
                            error=err_msg[:1000],
                        )
                    else:
                        logger.warning(
                            "重新解析被拒后回滚时文档行已查不到（软删？）doc=%s",
                            document_id,
                        )
                logger.warning(
                    "重新解析未被任务中心受理: doc=%s task=%s disposition=%s message=%s",
                    document_id, tc_plan["task_id"],
                    retry_result.disposition.value, retry_result.message,
                )
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
                    "error_message": err_msg[:1000],
                })
                raise ResourceConflictError(
                    message=err_msg,
                    details={
                        "document_id": document_id,
                        "task_id": tc_plan["task_id"],
                    },
                )
            logger.info(
                "重新解析已交任务中心重跑: doc=%s task=%s 名单=%s",
                document_id, tc_plan["task_id"],
                [node_id_for_step(code) for code in tc_step_codes],
            )
            return doc_dict

        # ── 下面的旧链路只服务「库类型不在 `SUPPORTED_KB_TYPES`」的库 ──

        # 确保 KB 编译 schema 存在（非阻塞）
        # 若已在指纹计算阶段解析过，优先复用缓存值
        schema = None
        # [jonex] openkb 不用 compiled schema；且 auto_compile=True 有副作用（凭空生成 schema），
        # 必须整段跳过，不能只是取了不传。
        schema_version = _cached_schema_ver if (not is_llm_wiki and _cached_schema_ver) else 0
        if not is_llm_wiki:
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
                execution_mode="parse_only" if is_llm_wiki else "reparse_strict",
                strict_push=not is_llm_wiki,
                content_generation=new_generation,
                schema_version=schema_version,     # openkb 下恒为 0
                old_rag_doc_ids=[] if is_llm_wiki else old_rag_doc_ids,
                idempotency_key=idempotency_key,  # [jonex] R1
                kb_type=kb_type,                  # [jonex] llm-wiki 产物分支（openkb→content.md / jonex_wiki→content_list.json）
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
        predicates = _OPENKB_PHASE_PREDICATE if kb_type in LLM_WIKI_TYPES else _PHASE_PREDICATE

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

        # 第 3 期生命周期：默认排除归档文档（归档 = 可恢复的下线，不应占浏览列表）；
        # 只有**显式**筛选归档（status=archived 或 phase 含 archived）时才保留。
        # 判据必须是 req 的原值，不能看上面翻译后的 conditions —— 否则
        # phase=["archived"] 会被 _phase_condition 翻译成等值谓词，排除条件照样加上，
        # 列表恒为空。
        explicit_archived = (
            req.status == DocStatus.ARCHIVED.value
            or bool(req.phase and DocStatus.ARCHIVED.value in req.phase)
        )
        if not explicit_archived:
            conditions.append(KnowledgeDocument.status != DocStatus.ARCHIVED.value)
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
        is_llm_wiki = kb_type in LLM_WIKI_TYPES

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)

            async def _count(extra: list) -> int:
                return await repo.count(tenant_id=tenant_id, extra_conditions=base + extra)

            # 互斥四桶（相加 = total）：每个文档恰好落进一桶，避免嵌套指标的对账困惑。
            # 处理中：待解析/解析中/入库中/待编译/编译中（还在跑）
            if is_llm_wiki:
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

    async def assert_document_readable(
        self, tenant_id: str, user_id: str | None, document_id: str,
    ) -> dict:
        """校验文档可读（B6 §5.2：网关 view-ticket 的前置校验）。

        只判**租户 + KB 空间/KB 授权**，不返回正文供调用方复用。
        密级判定由 capability 层的 classification_access_guard 负责（本方法
        只回答「这份文档在权限上是否存在并可读」）。

        未登录 / `anonymous` ⇒ 404；租户管理员、空间管理者/成员、
        任一有效 KB 授权 ⇒ 放行；其余一律 404（不泄露存在性）。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            doc = await KnowledgeDocumentRepository(session).get_by_id(
                document_id, tenant_id,
            )
            if doc is None:
                raise ResourceNotFoundError(message="文档不存在")
            knowledge_base_id = doc.knowledge_base_id

        if not user_id or user_id == "anonymous":
            raise ResourceNotFoundError(message="文档不存在")

        from .space_permission_service import (
            SPACE_MANAGER,
            SPACE_MEMBER,
            has_space_role,
            is_tenant_admin,
        )
        from .kb_permission_service import get_kb_grant_role

        if await is_tenant_admin(tenant_id, user_id):
            return {"document_id": document_id}

        from sqlalchemy import text as _sql_text

        async with get_db_session() as session:
            row = (await session.execute(
                _sql_text(
                    "SELECT space_id FROM knowledge_base.knowledge_info "
                    "WHERE id = :kb_id AND tenant_id = :tenant_id AND is_deleted = 0"
                ),
                {"kb_id": knowledge_base_id, "tenant_id": tenant_id},
            )).first()
        space_id = row[0] if row else None
        if space_id and await has_space_role(
            tenant_id, space_id, user_id, SPACE_MANAGER, SPACE_MEMBER,
        ):
            return {"document_id": document_id}
        if await get_kb_grant_role(tenant_id, knowledge_base_id, user_id):
            return {"document_id": document_id}

        raise ResourceNotFoundError(message="文档不存在")

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

    async def get_document_quality_report(
        self, tenant_id: str, document_id: str, knowledge_base_id: str = "",
    ) -> dict:
        """文档解析质量报告（单元二 D1）。

        归属先校验（跨租户 / 跨 KB 一律按不存在处理）。
        质量观测不在本线范围，恒返回 `supported=False`。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            doc = await KnowledgeDocumentRepository(session).get_by_id(
                document_id, tenant_id,
            )
        if doc is None or (
            knowledge_base_id and doc.knowledge_base_id != knowledge_base_id
        ):
            raise ResourceNotFoundError(
                message=translate("err.doc.not_found", fallback="知识文档不存在"),
            )
        return {
            "supported": False,
            "document_id": document_id,
            "report": None,
            "md_url": None,
        }

    async def get_document_source_spans(
        self,
        tenant_id: str,
        document_id: str,
        chunk_index: Optional[int] = None,
        expect: Optional[dict] = None,
    ) -> dict:
        """读引用片段在原文里的 bbox 坐标（打开原文时的框选定位）。

        本版本不接入引用坐标投影，恒降级为只跳页；**降级绝不抛错** —— 调用方是
        「点开引用」的交互路径，框选是增强，不是阅读原文的前置条件。
        """
        return {
            "document_id": document_id,
            "chunk_index": (chunk_index if isinstance(chunk_index, int)
                            and not isinstance(chunk_index, bool) else None),
            "spans": [],
            "degraded": True,
            "reason": "backend_not_supported",
        }

    async def get_document_quality_reports(
        self, tenant_id: str, knowledge_base_id: str, document_ids: list[str],
    ) -> dict:
        """批量质量摘要（列表页用）。返回 `{items: [{document_id, supported, verdict, reasons}]}`。

        归属过滤：不在本租户、或不属于本 KB 的 id **静默跳过**（批量接口不泄露
        存在性）。质量观测不在本线范围，恒返回 `supported=False`。
        """
        tenant_id = require_tenant(tenant_id)
        ids = list(dict.fromkeys(
            str(x).strip() for x in (document_ids or []) if str(x).strip()
        ))
        # 与引擎侧同一道闸（1..100）；超限/空一律 400，不静默截断。
        if not 1 <= len(ids) <= 100:
            raise InvalidParameterError(
                message="document_ids 数量必须在 1..100",
                details={"count": len(ids)},
            )
        async with get_db_session() as session:
            docs = await KnowledgeDocumentRepository(session).get_by_ids(
                ids, tenant_id,
            )
        by_id = {
            doc.id: doc for doc in docs
            if doc.knowledge_base_id == knowledge_base_id
        }
        items = []
        for doc_id in ids:
            if doc_id not in by_id:
                continue
            items.append({
                "document_id": doc_id,
                "supported": False,
                "verdict": None,
                "reasons": [],
            })
        return {"items": items}

    async def get_document_provenance(
        self, tenant_id: str, document_id: str, knowledge_base_id: str,
        limit: int = 100,
    ) -> dict:
        """文档参与产出的本体实体与关系（只读，供数据治理血缘消费）。

        口径：
        - 归属先校验（跨租户 / 跨 KB 一律 404），**失败不被 Neo4j 降级吞掉**；
        - 图库不可用（网络/驱动/瞬时可重试异常）→ 空列表 + warning，不阻塞调用方；
          只降级 ``DriverError`` / ``TransientError`` / ``OSError`` / 超时，
          ``ClientError``（如 ``CypherSyntaxError``）、``AttributeError``、
          ``TypeError`` 等查询或编程错误照常上抛（否则会被静默吞成「200 + 空血缘」）；
        - 按 ``limit + 1`` 拉取 → 归一/去重 → ``len(dedup) > limit`` 判截断 → 切片；
          先切片再去重会把「重复行占满配额」误报为截断；
        - ``limit`` 对外契约 1..200，非法值 400（dispatch 不做 int 转换）。
        """
        tenant_id = require_tenant(tenant_id)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _PROVENANCE_LIMIT_MAX
        ):
            raise InvalidParameterError(
                message=f"limit 必须是 1..{_PROVENANCE_LIMIT_MAX} 的整数",
                details={"limit": limit},
            )

        async with get_db_session() as session:
            doc = await KnowledgeDocumentRepository(session).get_by_id(
                document_id, tenant_id,
            )
        if doc is None or doc.knowledge_base_id != knowledge_base_id:
            raise ResourceNotFoundError(
                message=translate("err.doc.not_found", fallback="知识文档不存在"),
            )

        entities: list[dict] = []
        relations: list[dict] = []
        try:
            gdao = OntologyGraphRepository(get_neo4j_driver())
            entities, relations = await gdao.list_document_provenance(
                tenant_id, knowledge_base_id, document_id, limit=limit + 1,
            )
        except (DriverError, TransientError, OSError, asyncio.TimeoutError):
            logger.warning(
                "Neo4j provenance unavailable for tenant=%s kb=%s doc=%s",
                tenant_id, knowledge_base_id, document_id,
                exc_info=True,
            )
            entities, relations = [], []

        parsed_entities, parsed_relations = _parse_provenance_rows(
            entities, relations,
        )
        entities_truncated = len(parsed_entities) > limit
        relations_truncated = len(parsed_relations) > limit
        return {
            "entities": parsed_entities[:limit],
            "relations": parsed_relations[:limit],
            "entities_truncated": entities_truncated,
            "relations_truncated": relations_truncated,
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
        allow_filename_fallback: bool = True,
        audit_action: str = "delete_document",
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
            # ── [jonex] 2026-09-30（用户拍板）：**还有任务在跑就不许删** ──
            #
            # 上面那两道闸只看 `doc.status`，而任务中心接管中的文档**恰恰常常是「看着能操作」
            # 的稳定态**（`parsed` / `ready`）：Ontology 库的文档在「解析已完成、正在抽本体」
            # 那段就是 `ready` + 接管行在途。只看状态的话这段能删掉，而底下本体抽取的活
            # 还在跑 —— 这就是 CLAUDE.md 那句「接管行存在时，重新解析 / 删除文档 / 整库重置
            # 三个入口都会被拦」要表达的规则，此前这道闸只落在另外两个入口上。
            #
            # 判据与「重新解析」那道**同一个**（`exists_active`：`submitting` / `managed`
            # 都算在途），消息也**共用同一条**：从用户视角看，两件事的答复是同一句
            # 「它还在处理中，等它跑完」——真被卡住时的出口也一样（去任务页点「停止」，
            # 收口之后接管行随即交还，最长几秒）。
            #
            # ⚠️ 顺序：**必须排在 `set_status(DELETING)` 之前** —— 它是本方法第一处写库，
            # 排在它后面等于「已经改成删除中再拦」，那就白拦了。
            if await TaskCenterDocumentRepository(session).exists_active(
                document_id, tenant_id
            ):
                raise ResourceConflictError(
                    message=translate(
                        "err.doc.parsing_cannot_delete",
                        fallback="知识文档正在解析/入库中，请等待完成后再删除",
                    )
                )
            await repo.set_status(doc, DocStatus.DELETING)
            rag_doc_ids = list(doc.rag_doc_ids or [])
            kb_id = doc.knowledge_base_id or ""
            file_name = doc.file_name or ""
            storage_key = doc.storage_key
            storage_backend = doc.storage_backend

        rag_doc_ids = await self._resolve_rag_doc_ids(
            tenant_id=tenant_id,
            document_id=document_id,
            kb_id=kb_id,
            file_name=file_name,
            rag_doc_ids=rag_doc_ids,
            allow_filename_fallback=allow_filename_fallback,
        )

        # 索引清理链与归档共用（归档只跳过下面的对象存储 + 软删）
        await self._remove_document_indexes(
            tenant_id=tenant_id,
            document_id=document_id,
            kb_id=kb_id,
            file_name=file_name,
            rag_doc_ids=rag_doc_ids,
        )
        # 删除链已清索引：打标，避免删库补偿器重复清理同一文档。
        # 归档不在这里打标（归档后还能恢复重建索引）。
        await self.mark_indexes_cleaned(tenant_id, document_id)

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
            # 软删前清归档时间：宽限销毁后不应再留下「已归档」的时间戳
            doc.archived_at = None
            await repo.set_status(doc, DocStatus.DELETED)
            await repo.delete_soft(doc, tenant_id)

        audit_payload = {
            "tenant_id": tenant_id,
            "user_id": _audit_user_id(user_id),
            "username": username,
            "ip": ip,
            "log_type": "OPERATION",
            "action": audit_action,
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(document_id),
        }
        if audit_action == "destroy_document":
            # 销毁与普通删除共用同一条删除链，审计侧要能区分来源
            audit_payload["request_params"] = {"source": "destroy"}
        schedule_emit(audit_payload)

        return {"id": document_id, "deleted": True}

    # ── 第 3 期生命周期：索引清理 / 归档 / 恢复 / 销毁 ──

    async def _resolve_rag_doc_ids(
        self,
        *,
        tenant_id: str,
        document_id: str,
        kb_id: str,
        file_name: str,
        rag_doc_ids: list[str],
        allow_filename_fallback: bool = True,
    ) -> list[str]:
        """把文档行上的 `rag_doc_ids` 补齐成可清理的清单。

        `rag_doc_ids` 可能为空（insert 不返回 doc_ids、对账未及时补充时），
        此时从 LightRAG 存储反查，确保已入库的内容能被清理。

        ⚠ 先按 `doc=<id>` 锚点反查（部分文档的 `file_name` 为 null，关键词筛选永远
        筛不到它们 —— 这正是这类文档删除后残留 LightRAG 切片的根因）；未命中
        （`[]`）才退回文件名兜底。查询失败（`None`）
        仍按删除链路的既有语义 best-effort 继续，但记 warning。
        """
        ids = list(rag_doc_ids or [])
        if ids or not kb_id:
            return ids

        ids_by_doc = await find_rag_doc_ids_by_document(
            get_rag_client,
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            document_id=document_id,
        )
        if ids_by_doc:
            ids = list(ids_by_doc)
        elif ids_by_doc is None:
            logger.warning(
                "LightRAG lookup by doc_id failed for document %s; "
                "index cleanup proceeds with best-effort", document_id,
            )
        # 文件名兜底只对普通删除开放：归档清空 rag_doc_ids 后，销毁再也拿不到
        # doc= 锚点；此时按文件名扫会命中**同 KB 同名不同内容**的文档。
        if not ids and file_name and allow_filename_fallback:
            ids = await self._lookup_rag_doc_ids(
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                file_name=file_name,
                document_id=document_id,
            )
        return ids

    async def _remove_document_indexes(
        self,
        *,
        tenant_id: str,
        document_id: str,
        kb_id: str,
        file_name: str,
        rag_doc_ids: list[str],
        kb_type: str | None = None,
    ) -> None:
        """从检索索引移除文档（不删对象存储、不软删文档行）。

        删除与归档共用的唯一索引清理链：LightRAG batch delete + 逐条兜底、
        openkb / jonex_wiki `remove_document`、非 LLM_WIKI 的 Neo4j 清理。
        归档复用它、跳过调用方的「对象存储删除 + 软删」两步即可恢复。

        `kb_type` 可由调用方显式传入：删库后的补偿器面对的是**已软删**的文档与 KB 行，
        KB 行已删 → kb_type 查不到，必须用删除前的快照值。
        """

        # [jonex] batch delete 失败的 doc 兜底重试清单；delete_batch 抛异常时
        # 全部 doc 视为失败（批量删除未发生）。默认空，避免后续循环误删。
        batch_failed: list[str] = []

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
                batch_failed = result.get("failed", [])
                if accepted:
                    logger.info(
                        "RAG batch delete accepted %d/%d doc_ids for doc %s",
                        len(accepted), len(rag_doc_ids), document_id,
                    )
                if batch_failed:
                    logger.warning(
                        "RAG batch delete failed %d doc_ids for doc %s: %s",
                        len(batch_failed), document_id, batch_failed,
                    )
            except Exception:
                batch_failed = list(rag_doc_ids)
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
        if kb_type is None:
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
        elif kb_type == "jonex_wiki":
            # [jonex] 删除必须按 kb_type 分流到 jonex_wiki client,否则 openkb client
            # 会去 openkb 引擎删一个不存在的 document_id,导致 jonex_wiki 引擎侧文档
            # 与解析/编译产物残留,同名重传时报「同知识库已存在同名文档」。
            from .jonex_wiki_service import JonexWikiService  # [jonex] lazy import 避免循环依赖
            compiler = JonexWikiService()
            try:
                await compiler.remove_document(
                    kb_name=kb_id, document_id=document_id,
                    tenant_id=tenant_id, kb_id=kb_id,
                )
            except Exception:
                logger.warning("Failed to remove Jonex Wiki document %s", document_id, exc_info=True)
        else:
            # [jonex] 仅对 batch delete 失败的 doc 逐条重试。此前无条件全量
            # 逐条 delete，与 batch delete 重复——batch 已删的 doc 全部命中
            # "Document not found"，纯冗余且污染 LightRAG 日志。
            for rag_doc_id in batch_failed:
                try:
                    await get_rag_client().delete(
                        rag_doc_id, tenant_id=tenant_id, knowledge_base_id=kb_id
                    )
                except Exception:
                    logger.warning("Failed to delete RAG document %s", rag_doc_id, exc_info=True)

        # 清理 Neo4j 本体图谱中该文档关联的实体节点和关系
        # [jonex] llm-wiki 文档从不写 Neo4j（parse_only 管线），跳过——纯多余调用
        # （每次删除省一次 Neo4j 往返；Neo4j 不可用时也不再产生无关 warning）
        if kb_type not in LLM_WIKI_TYPES:
            try:
                gdao = OntologyGraphRepository(get_neo4j_driver())
                await gdao.delete_by_document(tenant_id, kb_id, document_id)
            except Exception:
                logger.warning("Neo4j cleanup failed for document %s", document_id, exc_info=True)

    async def mark_indexes_cleaned(
        self, tenant_id: str, document_id: str,
    ) -> None:
        """标记该文档的检索索引已清理（删库补偿器据此跳过，不重复调用引擎）。

        必须在 `_remove_document_indexes` **之后**调用；调用失败不抛出 ——
        补偿器最多重复清理一次（幂等），不值得把删除链路拖失败。
        """
        try:
            async with get_db_session() as session:
                await session.execute(
                    text("""
                        UPDATE knowledge_base.knowledge_documents
                           SET indexes_cleaned_at = now(), updated_at = now()
                         WHERE tenant_id = :tenant_id AND id = :document_id
                    """),
                    {"tenant_id": tenant_id, "document_id": document_id},
                )
                await session.commit()
        except Exception:
            logger.warning(
                "Failed to mark indexes_cleaned_at for doc %s", document_id,
                exc_info=True,
            )

    # ── 文档版本链（版本链 + 新版生效） ────────────────────────────

    async def list_document_versions(
        self, tenant_id: str, document_id: str,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            await KnowledgeDocumentRepository(session).get_required(
                document_id, tenant_id,
            )
            rows = await KnowledgeDocumentVersionRepository(session).list_for_document(
                tenant_id, document_id,
            )
        return {
            "items": [row.to_dict() for row in rows],
            "total": len(rows),
        }

    async def upload_document_version(
        self,
        tenant_id: str,
        document_id: str,
        data: dict,
        *,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> dict:
        """上传文档新版本：旧版存档、新版生效并重新解析/建索引。"""
        tenant_id = require_tenant(tenant_id)
        storage_key = str(data.get("storage_key") or "").strip()
        kb_id = str(data.get("knowledge_base_id") or "").strip()
        if not storage_key or not kb_id:
            raise InvalidParameterError(message="storage_key 与 knowledge_base_id 不能为空")
        storage_backend = str(data.get("storage_backend") or "local").strip().lower()
        file_name = str(data.get("file_name") or storage_key.rsplit("/", 1)[-1])
        mime_type = data.get("mime_type")
        file_size = int(data.get("file_size") or 0)

        content_hash = await self._compute_source_hash(storage_backend, storage_key)
        if storage_backend == "local":
            from jonex_core.common.object_storage import get_object_storage

            file_path = get_object_storage().fs_path(storage_key) or storage_key
        else:
            file_path = storage_key

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
            if (doc.knowledge_base_id or "") != kb_id:
                raise InvalidParameterError(message="文档不属于该知识库")
            if doc.status in (
                DocStatus.ARCHIVED.value, DocStatus.DELETED.value,
            ):
                raise ResourceConflictError(message="已归档或已删除的文档不能上传新版本")
            if doc.status in (
                DocStatus.PARSING.value, DocStatus.INGESTING.value,
                DocStatus.DELETING.value,
            ):
                raise ResourceConflictError(message="文档正在解析/入库/删除中，请稍后再上传新版本")
            # 与 reparse 同一道闸：任务中心接管中的文档看着 ready，底下可能还在抽本体。
            # 必须在**任何写入之前**拦，否则版本行/文档行已切换、reparse 才报 1003，
            # 留下「文件换了但索引没重建」的半成品。
            if await TaskCenterDocumentRepository(session).exists_active(
                document_id, tenant_id,
            ):
                raise ResourceConflictError(
                    message="文档正在由任务中心处理，请等当前步骤结束后再上传新版本",
                )

            version_repo = KnowledgeDocumentVersionRepository(session)
            existing = await version_repo.list_for_document(tenant_id, document_id)
            if not existing:
                # 防御性补 v1（迁移回填覆盖存量；此分支只针对异常缺行）
                await version_repo.create(
                    KnowledgeDocumentVersion(
                        tenant_id=tenant_id,
                        document_id=document_id,
                        version_no=1,
                        file_name=doc.file_name,
                        file_size=doc.file_size or 0,
                        mime_type=doc.mime_type,
                        storage_backend=doc.storage_backend or "local",
                        storage_key=doc.storage_key,
                        content_hash=doc.content_hash,
                        source="backfill",
                    )
                )
            version_no = await version_repo.next_version_no(tenant_id, document_id)
            version = await version_repo.create(
                KnowledgeDocumentVersion(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    version_no=version_no,
                    file_name=file_name,
                    file_size=file_size,
                    mime_type=mime_type,
                    storage_backend=storage_backend,
                    storage_key=storage_key,
                    content_hash=content_hash or None,
                    source="upload",
                    created_by=username or user_id,
                )
            )
            version_dict = version.to_dict()

            # 文档行切到新版本（旧 storage_key 仍由旧版本行引用，不删对象）
            doc.file_name = file_name
            doc.file_size = file_size or 0
            doc.mime_type = mime_type
            doc.storage_key = storage_key
            doc.storage_backend = storage_backend
            doc.file_path = file_path
            doc.content_hash = content_hash or None
            await session.flush()
            await session.commit()

        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": _audit_user_id(user_id),
            "username": username,
            "ip": ip,
            "log_type": "OPERATION",
            "action": "upload_document_version",
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(document_id),
            "request_params": {
                "knowledge_base_id": kb_id,
                "version_no": version_no,
                "file_name": file_name,
            },
        })

        # 新版生效：强制重新解析（force 绕过「源文件未变化」的 hash 跳过）。
        # 版本行与文档行已提交，reparse 的偶发失败不能把「上传成功」改写成失败；
        # 失败原因随响应返回，前端提示「已上传，但重新解析失败」。
        reparse: dict = {}
        reparse_error: str | None = None
        try:
            reparse = await self.reparse_document(
                tenant_id,
                document_id,
                user_id=user_id,
                username=username,
                ip=ip,
                force=True,
                reason="new_version",
            )
        except Exception as exc:  # noqa: BLE001 —— 版本已落库，这里只做提示
            logger.warning(
                "上传新版本后重新解析失败 doc=%s version=%s: %s",
                document_id, version_no, exc, exc_info=True,
            )
            reparse_error = getattr(exc, "message", None) or str(exc)

        return {
            "document_id": document_id,
            "version": version_dict,
            "reparse": reparse,
            **({"reparse_error": reparse_error} if reparse_error else {}),
        }

    async def get_document_version_url(
        self, tenant_id: str, document_id: str, version_no: int,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        try:
            version_int = int(version_no)
        except (TypeError, ValueError):
            raise InvalidParameterError(message="version_no 必须是正整数")
        if version_int < 1:
            raise InvalidParameterError(message="version_no 必须是正整数")

        async with get_db_session() as session:
            await KnowledgeDocumentRepository(session).get_required(
                document_id, tenant_id,
            )
            row = await KnowledgeDocumentVersionRepository(session).get_version(
                tenant_id, document_id, version_int,
            )
        if row is None:
            raise ResourceNotFoundError(message="文档版本不存在")

        from jonex_core.common.object_storage import (
            get_object_storage,
            get_object_storage_for,
        )

        storage = (
            get_object_storage_for(row.storage_backend)
            if row.storage_backend else get_object_storage()
        )
        url = await storage.presigned_url(row.storage_key, tenant_id, expires=900)
        return {
            "version_no": row.version_no,
            "file_name": row.file_name,
            "mime_type": row.mime_type,
            "url": url,
        }

    async def archive_document(
        self,
        tenant_id: str,
        document_id: str,
        knowledge_base_id: str,
        *,
        reason: str = "manual",
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> dict:
        """归档文档：清检索索引，保留对象存储与文档行。

        归档是「可恢复的下线」—— 与删除的唯一区别就是**不删原始文件、不软删行**。
        幂等：已归档直接返回成功（调度器可能重复扫到同一篇）。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_by_id(document_id, tenant_id)
            if doc is None or doc.knowledge_base_id != knowledge_base_id:
                raise ResourceNotFoundError(
                    message=translate("err.doc.not_found", fallback="知识文档不存在"),
                )
            if doc.status == DocStatus.ARCHIVED.value:
                return {"id": document_id, "status": "archived", "archived": True}
            if doc.status == DocStatus.DELETING.value:
                raise ResourceConflictError(
                    message=translate(
                        "err.doc.deleting_in_progress", fallback="知识文档正在删除中",
                    ),
                )
            if doc.status == DocStatus.DELETED.value:
                raise ResourceConflictError(
                    message=translate(
                        "err.doc.deleted_cannot_archive", fallback="知识文档已删除，无法归档",
                    ),
                )
            if doc.status in (
                DocStatus.PENDING.value,
                DocStatus.PARSING.value,
                DocStatus.INGESTING.value,
            ):
                raise ResourceConflictError(
                    message=translate(
                        "err.doc.parsing_cannot_archive",
                        fallback="知识文档正在解析/入库中，请等待完成后再归档",
                    ),
                )
            # 与删除同一个判据：任务中心接管中的文档**看着能操作**（ready/parsed），
            # 底下可能还在抽本体 —— 必须在任何写库之前拦住。
            if await TaskCenterDocumentRepository(session).exists_active(
                document_id, tenant_id
            ):
                raise ResourceConflictError(
                    message=translate(
                        "err.doc.task_center_managed_archive",
                        fallback="该文档正在由任务中心处理，请等当前步骤结束后再归档",
                    ),
                )
            # 条件迁移的基准状态：索引清理耗时期间它可能被 reparse/delete 推进
            from_status = doc.status
            rag_doc_ids = list(doc.rag_doc_ids or [])
            kb_id = doc.knowledge_base_id or ""
            file_name = doc.file_name or ""

        rag_doc_ids = await self._resolve_rag_doc_ids(
            tenant_id=tenant_id,
            document_id=document_id,
            kb_id=kb_id,
            file_name=file_name,
            rag_doc_ids=rag_doc_ids,
        )
        await self._remove_document_indexes(
            tenant_id=tenant_id,
            document_id=document_id,
            kb_id=kb_id,
            file_name=file_name,
            rag_doc_ids=rag_doc_ids,
        )

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            moved = await repo.transition_status(
                document_id,
                tenant_id,
                from_status=from_status,
                to_status=DocStatus.ARCHIVED,
                reason=reason,
                archived_at=datetime.now(),
                clear_rag_doc_ids=True,
                kb_id=kb_id,
            )
            if not moved:
                raise ResourceConflictError(
                    message=translate(
                        "err.doc.status_changed",
                        fallback="文档状态已变化，请重试",
                    ),
                )

        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": _audit_user_id(user_id),
            "username": username,
            "ip": ip,
            "log_type": "OPERATION",
            "action": "archive_document",
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(document_id),
        })
        return {"id": document_id, "status": "archived", "archived": True}

    async def _revert_to_archived(
        self,
        tenant_id: str,
        document_id: str,
        *,
        archived_at: datetime | None = None,
    ) -> None:
        """恢复失败的回滚：文档行回到归档态。

        ⚠ `archived_at` 用恢复前的快照，**不要刷新成 now()** —— 否则每次恢复
        失败都会重置宽限销毁的时钟，一份坏文档可以靠反复点恢复永久躲过销毁。
        `error_message` 一并清空：归档态不该携带一次失败恢复的报错。
        """
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
            doc = await repo.set_status(
                doc, DocStatus.ARCHIVED, reason="restore_failed",
            )
            doc.error_message = None
            doc.archived_at = archived_at or datetime.now()
            await session.flush()

    async def restore_document(
        self,
        tenant_id: str,
        document_id: str,
        knowledge_base_id: str,
        *,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> dict:
        """恢复已归档文档：重建检索索引，清 `archived_at`。

        流转链是**显式三段**：`archived -> pending`（本次条件迁移，留事件）
        → `pending -> parsing`（reparse 重置，留事件）→ 解析收尾由既有管线推进。
        `pending` 这一段不是多余的中转：它是「已受理、还没开跑」的可观测点 ——
        直接 `archived -> parsing` 的话，用户在恢复失败时看到的只有一次
        「状态变过又变回去」，中间发生了什么无从判断。

        reparse 用 `allow_pending=True` 放行这一档 `PENDING`（互斥闸默认拒绝，
        详见 `reparse_document`）。失败回滚 `pending -> archived` 同样留事件
        （reason=`restore_failed`），并保留原 `archived_at`。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_by_id(document_id, tenant_id)
            if doc is None or doc.knowledge_base_id != knowledge_base_id:
                raise ResourceNotFoundError(
                    message=translate("err.doc.not_found", fallback="知识文档不存在"),
                )
            if doc.status != DocStatus.ARCHIVED.value:
                raise ResourceConflictError(
                    message=translate(
                        "err.doc.not_archived",
                        fallback="仅已归档的文档可以恢复",
                    ),
                )
            # 失败回滚要保留的宽限时钟基准
            original_archived_at = doc.archived_at
            # 先做 `archived -> pending` 条件迁移：拿到「已受理」这个可观测点，
            # 也让下面 reparse 的互斥闸能凭 `allow_pending=True` 明确放行这一档。
            # 与 validation 同一个 session：不通过就从这里直接报 409。
            moved = await repo.transition_status(
                document_id,
                tenant_id,
                from_status=DocStatus.ARCHIVED,
                to_status=DocStatus.PENDING,
                reason="manual_restore",
                kb_id=knowledge_base_id,
            )
            if not moved:
                raise ResourceConflictError(
                    message=translate(
                        "err.doc.status_changed",
                        fallback="文档状态已变化，请重试",
                    ),
                )

        try:
            result = await self.reparse_document(
                tenant_id,
                document_id,
                user_id=user_id,
                username=username,
                ip=ip,
                force=True,
                allow_pending=True,
                reason="manual_restore",
            )
        except Exception:
            try:
                await self._revert_to_archived(
                    tenant_id, document_id, archived_at=original_archived_at,
                )
            except Exception:
                logger.exception(
                    "恢复失败后回滚归档态也失败：doc=%s", document_id,
                )
            schedule_emit({
                "tenant_id": tenant_id,
                "user_id": _audit_user_id(user_id),
                "username": username,
                "ip": ip,
                "log_type": "OPERATION",
                "action": "restore_document_failed",
                "outcome": "FAILED",
                "service_name": "knowledge_base",
                "resource": ResourceType.DOCUMENT.value,
                "resource_id": str(document_id),
            })
            raise

        final_status = str((result or {}).get("status") or "")
        if final_status == DocStatus.FAILED.value:
            try:
                await self._revert_to_archived(
                    tenant_id, document_id, archived_at=original_archived_at,
                )
            except Exception:
                logger.exception(
                    "恢复失败后回滚归档态也失败：doc=%s", document_id,
                )
            schedule_emit({
                "tenant_id": tenant_id,
                "user_id": _audit_user_id(user_id),
                "username": username,
                "ip": ip,
                "log_type": "OPERATION",
                "action": "restore_document_failed",
                "outcome": "FAILED",
                "service_name": "knowledge_base",
                "resource": ResourceType.DOCUMENT.value,
                "resource_id": str(document_id),
            })
            raise ResourceConflictError(
                message=translate(
                    "err.doc.restore_failed",
                    fallback="恢复失败，文档已回到归档状态",
                ),
            )

        # 恢复成功：清归档时间（reparse 已把 status 推到 parsing/ready）
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
            doc.archived_at = None
            await session.flush()

        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": _audit_user_id(user_id),
            "username": username,
            "ip": ip,
            "log_type": "OPERATION",
            "action": "restore_document",
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(document_id),
        })
        return {
            "id": document_id,
            "status": final_status or DocStatus.PENDING.value,
            "restored": True,
        }

    async def destroy_document(
        self,
        tenant_id: str,
        document_id: str,
        knowledge_base_id: str,
        *,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        ip: Optional[str] = None,
        system: bool = False,
    ) -> dict:
        """销毁已归档文档：不可恢复的完整删除（含对象存储与文档行）。

        **租户管理员硬校验**：能力层的 `_SPACE_WRITE_ACTIONS` 只保证空间管理者，
        销毁是不可逆操作，必须再按 `tenant:admin` / `platform:admin` 判一次。

        `system=True` 跳过该判定，**仅供进程内调度直接调用**（任务 5 的保留
        扫描），不经能力层 —— 能力层永远不传这个参数。
        """
        tenant_id = require_tenant(tenant_id)
        from .space_permission_service import is_tenant_admin

        if not system and not await is_tenant_admin(tenant_id, user_id):
            raise PermissionDeniedError(
                message=translate(
                    "err.doc.destroy_requires_admin",
                    fallback="仅租户管理员可以销毁已归档文档",
                ),
            )

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_by_id(document_id, tenant_id)
            if doc is None:
                # 幂等出口：已软删的行 get_by_id 看不到；再点一次销毁应当成功
                meta = await repo.get_status_including_deleted(document_id, tenant_id)
                if meta is None or meta[1] != knowledge_base_id:
                    raise ResourceNotFoundError(
                        message=translate("err.doc.not_found", fallback="知识文档不存在"),
                    )
                if meta[0] == DocStatus.DELETED.value:
                    return {"id": document_id, "status": "deleted", "deleted": True}
                raise ResourceNotFoundError(
                    message=translate("err.doc.not_found", fallback="知识文档不存在"),
                )
            if doc.knowledge_base_id != knowledge_base_id:
                raise ResourceNotFoundError(
                    message=translate("err.doc.not_found", fallback="知识文档不存在"),
                )
            if doc.status != DocStatus.ARCHIVED.value:
                raise ResourceConflictError(
                    message=translate(
                        "err.doc.not_archived_destroy",
                        fallback="仅已归档的文档可以销毁",
                    ),
                )

        # 删除本身已记审计（action 区分成 destroy_document）；这里不再重复记一条。
        return await self.delete_document(
            tenant_id,
            document_id,
            {"knowledge_base_id": knowledge_base_id},
            user_id=user_id,
            username=username,
            ip=ip,
            # 归档已清空 rag_doc_ids：**禁止文件名兜底**，否则会误删同 KB 同名文档
            # 的切片。仍保留 delete_document 里的 `doc=` 锚点反查。
            allow_filename_fallback=False,
            audit_action="destroy_document",
        )

    async def set_document_folder(
        self,
        tenant_id: str,
        document_id: str,
        req: SetDocumentFolderRequest | dict,
        *,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        ip: Optional[str] = None,
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

            schedule_emit({
                "tenant_id": tenant_id,
                "user_id": _audit_user_id(user_id),
                "username": username,
                "ip": ip,
                "log_type": "OPERATION",
                "action": "set_document_folder",
                "outcome": "SUCCESS",
                "service_name": "knowledge_base",
                "resource": ResourceType.DOCUMENT.value,
                "resource_id": str(document_id),
                "request_params": {
                    "knowledge_base_id": knowledge_base_id,
                    "folder_id": folder_id,
                },
            })

            return doc.to_dict()

    async def batch_set_document_folder(
        self,
        tenant_id: str,
        req: BatchMoveDocumentsRequest | dict,
        *,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        ip: Optional[str] = None,
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

            schedule_emit({
                "tenant_id": tenant_id,
                "user_id": _audit_user_id(user_id),
                "username": username,
                "ip": ip,
                "log_type": "OPERATION",
                "action": "batch_set_document_folder",
                "outcome": "SUCCESS",
                "service_name": "knowledge_base",
                "resource": ResourceType.DOCUMENT.value,
                "resource_id": str(document_ids[0]),
                "request_params": {
                    "knowledge_base_id": knowledge_base_id,
                    "folder_id": folder_id,
                    "document_ids": document_ids,
                    "moved_count": moved,
                    "skipped_count": skipped,
                },
            })

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

        用于 rag_doc_ids 为空时的兜底（insert 不返回 doc_ids、reparse 覆盖后对账
        未及时补充）。分页与匹配统一在 `rag_storage_lookup` 内实现——此前本地副本
        把 page_size 写成 500（LightRAG 约束 [10, 200]）、且读不存在的 `items` 键，
        两处都让兜底恒失败，删除遂静默跳过 LightRAG 清理。
        """
        return await find_rag_doc_ids(
            get_rag_client,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            file_name=file_name,
            document_id=document_id,
        )

    # ── [jonex] kb_type 查询 → 公共 helper ──

    async def _get_kb_type(self, tenant_id: str, kb_id: str) -> str:
        """[jonex] 查询 KB 的 kb_type（转调 kb_type_service）。"""
        from .kb_type_service import get_kb_type
        return await get_kb_type(tenant_id, kb_id)

    async def _get_kb_ingest_backend(self, tenant_id: str, kb_id: str) -> str | None:
        """[jonex] 查询 KB 行的库级派单值（转调 kb_type_service，2026-10-09）。

        `None` 是**合法值**（= 跟随服务端开关 `KB_INGEST_BACKEND`），不是「查不到」
        —— 两者对 `resolve_backend` 是同一件事，所以不需要 or_none 那种二元 API。
        """
        from .kb_type_service import get_kb_ingest_backend
        return await get_kb_ingest_backend(tenant_id, kb_id)


__all__ = ["DocumentService"]
