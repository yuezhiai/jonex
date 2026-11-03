"""解析后相似检测：取解析文本、签名、全库扫描与合并建议。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from jonex_core.capability.atomic.rag.client import get_rag_client
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.config import get_config
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.object_storage import (
    build_object_key,
    build_parse_md_key,
    get_object_storage,
    get_object_storage_for,
)
from jonex_core.common.tenant import require_tenant

from . import ingest_backend
from .document_service import DocumentService, _audit_user_id, schedule_emit
from .similarity_signature import estimate_similarity, simhash64
from ..models.document import DocStatus, KnowledgeDocument, KnowledgeDocumentVersion
from ..models.knowledge_info import KnowledgeInfo
from ..models.similarity import (
    STATUS_DISMISSED, STATUS_MERGED, STATUS_PENDING, STATUS_SUPERSEDED,
)
from ..repository.document_repository import KnowledgeDocumentRepository
from ..repository.document_version_repository import KnowledgeDocumentVersionRepository
from ..repository.knowledge_info_repository import KnowledgeInfoRepository
from ..repository.similarity_repository import KnowledgeSimilarityRepository
from ..repository.task_center_repository import TaskCenterDocumentRepository
from .kb_type_service import LLM_WIKI_TYPES, get_kb_type_or_none

logger = logging.getLogger(__name__)

_scan_sem: asyncio.Semaphore | None = None

# I1：create_task 的返回值必须被强引用，否则任务对象可能在完成前被 GC 回收。
# 完成回调负责摘除，集合不会无限增长。
_pending_scan_tasks: set[asyncio.Task] = set()


def _get_scan_sem() -> asyncio.Semaphore:
    """惰性创建：模块导入时不绑定事件循环。"""
    global _scan_sem
    if _scan_sem is None:
        _scan_sem = asyncio.Semaphore(2)
    return _scan_sem


def _ordered_chunk_contents(chunks: list[dict]) -> list[str]:
    """把 chunk 列表整理成**跨文档可比**的稳定文本顺序。

    相似检测要拿两篇文档的文本做逐行比对，顺序不一致会让同一份内容被判低相似
    （实测同一对文档：乱序 0.46、规范序 0.98）。引擎侧顺序有两处不可靠：

    * 通用 RAG 读口（LightRAG 投影）返回顺序不稳定，`chunk_order_index`
      恒为 0；引擎自己的 chunk 投影才有可靠的 `chunk_index`（调用方映射成
      `chunk_order_index` 传进来）；
    * 其余链路只有部分返回 `chunk_order_index`。

    因此：序号**完整且互不相同**时按序号排；否则按文本排序（内容集合相同则结果
    必然相同，与返回顺序无关）。
    """
    rows = [
        ((c.get("content") or "").strip(), c.get("chunk_order_index"))
        for c in chunks
    ]
    rows = [(text, idx) for text, idx in rows if text]
    if not rows:
        return []
    indexes = [idx for _text, idx in rows]
    if all(isinstance(i, int) and not isinstance(i, bool) for i in indexes) and (
        len(set(indexes)) == len(indexes)
    ):
        rows.sort(key=lambda r: r[1])
    else:
        rows.sort(key=lambda r: r[0])
    return [text for text, _idx in rows]


def _compile_state(doc: Any, kb_type: str) -> str:
    if kb_type in LLM_WIKI_TYPES:
        return "compiled" if doc.llm_wiki_compile_status == "compiled" else (
            doc.llm_wiki_compile_status or "pending"
        )
    return {
        "ready": "compiled",
        "extracting": "compiling",
        "failed": "failed",
    }.get(doc.ontology_status or "", "pending")


def _status_value(status: Any) -> str:
    return status.value if hasattr(status, "value") else (status or "")


def rank_candidates(
    source_sig: int, rows, valid_ids: set[str], suppressed: set[str],
    *, candidate_threshold: float, top_k: int,
) -> list[tuple[float, str]]:
    ranked = []
    for row in rows:
        if row.simhash is None:  # unavailable 哨兵
            continue
        if row.document_id not in valid_ids or row.document_id in suppressed:
            continue
        score = estimate_similarity(source_sig, row.simhash)
        if score >= candidate_threshold:
            ranked.append((score, row.document_id))
    ranked.sort(reverse=True)
    return ranked[:top_k]


class DocumentSimilarityService:
    async def _kb_type(self, tenant_id: str, kb_id: str) -> str | None:
        """查不到（KB 已删/DB 抖动）返回 None：不得据此写 unavailable 哨兵。"""
        try:
            return await get_kb_type_or_none(tenant_id, kb_id)
        except Exception:
            logger.warning(
                "KB 类型读取失败，本次跳过相似检测: kb=%s", kb_id, exc_info=True,
            )
            return None

    async def _mark_signature_unavailable(self, doc: Any, source: str) -> None:
        """记录「已尝试但确实无可用文本」，避免回填每轮重复挑中同一批文档。"""
        async with get_db_session() as session:
            repo = KnowledgeSimilarityRepository(session)
            await repo.upsert_signature(
                doc.tenant_id, doc.knowledge_base_id, doc.id,
                simhash=None, text_chars=0,
                text_source=f"unavailable:{source}"[:32],
            )
            await session.commit()

    async def _load_text(self, doc: Any) -> tuple[str, str, bool]:
        """返回 (文本, 来源, 是否成功读取)。

        `ok=False` 表示**读取失败**（网络/存储/引擎异常）：读失败由调用方写
        `unavailable:read` 哨兵，由 TTL 回填重试；`RETRY_MINUTES<=0` 时该哨兵
        永久（仅重新解析可恢复）。
        """
        kb_type = await self._kb_type(doc.tenant_id, doc.knowledge_base_id)
        if kb_type is None:
            return "", "", False
        if kb_type in LLM_WIKI_TYPES:
            try:
                from .llm_wiki_artifacts import load_llm_wiki_blocks

                blocks, source = await asyncio.to_thread(
                    load_llm_wiki_blocks, kb_type, doc.id,
                )
                # wiki 的 content.md/content_list.json 在**解析阶段**就已落盘，
                # 这里读到空 blocks 即「确实无文本」，交给 compute_signature 写
                # unavailable 哨兵（不是读取失败，也不影响合并需 compiled 的判定）。
                return (
                    "\n\n".join(blocks),
                    f"wiki:{source}" if source else "wiki",
                    True,
                )
            except Exception:
                logger.warning("wiki 文本读取失败: doc=%s", doc.id, exc_info=True)
                return "", "", False

        try:
            result = await get_rag_client().get_doc_chunks(
                document_id=doc.id,
                knowledge_base_id=doc.knowledge_base_id,
                tenant_id=doc.tenant_id,
            )
            contents = _ordered_chunk_contents(result.get("chunks") or [])
            return "\n\n".join(contents), "chunks", True
        except Exception:
            logger.warning("chunks 读取失败: doc=%s", doc.id, exc_info=True)
            return "", "", False

    async def compute_signature(
        self, doc: Any, *, text: str | None = None, source: str | None = None,
    ) -> bool:
        if text is None:
            text, source, ok = await self._load_text(doc)
            if not ok:
                # 读取瞬时失败：**也落 unavailable 哨兵**（`unavailable:read`）。
                # 不落的话，该文档在签名表里「无行」，会永远占据
                # `list_documents_missing_signature` 的 `created_at ASC` 队头，
                # 把后面真正缺签名的文档饿死。落哨兵后由 TTL 重试 SQL 兜底重算。
                await self._mark_signature_unavailable(doc, "read")
                return False
        elif source is None:
            source = "reused"
        cfg = get_config()
        cap = int(getattr(cfg, "DOCUMENT_SIMILARITY_TEXT_CAP", 2_000_000))
        if len(text) > cap:
            text = text[:cap]
        value = await asyncio.to_thread(
            simhash64,
            text,
            limit=int(getattr(
                cfg, "DOCUMENT_SIMILARITY_SIGNATURE_SHINGLES", 200_000,
            )),
        )
        if value is None:
            await self._mark_signature_unavailable(doc, source or "unavailable")
            return False
        async with get_db_session() as session:
            repo = KnowledgeSimilarityRepository(session)
            await repo.upsert_signature(
                doc.tenant_id, doc.knowledge_base_id, doc.id,
                simhash=value, text_chars=len(text), text_source=source,
            )
            await session.commit()
        return True

    async def _persist_hits(
        self, tenant_id: str, kb_id: str, source_id: str,
        hits: list[tuple[float, str, str]],
        *, source_created_at=None, target_created_at: dict | None = None,
        preserve_existing: bool = False,
    ) -> None:
        """落建议。同一对只保留一条：source 恒为**较晚创建**的文档。

        `preserve_existing=True` 用于本轮有候选读取失败：跳过 supersede，
        只更新本轮可读命中的建议，未读到的候选保留旧建议。
        """
        async with get_db_session() as session:
            repo = KnowledgeSimilarityRepository(session)
            if not preserve_existing:
                # source 与 target 两侧都清：target 重解析后旧建议不得残留。
                await repo.supersede_pending_for_document(tenant_id, source_id)
            for ratio, target_id, method in hits:
                src, tgt = source_id, target_id
                if (
                    source_created_at is not None
                    and target_created_at
                    and target_created_at.get(target_id) is not None
                    and (target_created_at[target_id], target_id)
                    > (source_created_at, source_id)
                ):
                    src, tgt = target_id, source_id
                # 只有源文档（本轮扫描文档）重解析才允许复活已忽略的建议；
                # 翻转方向时冲突行保留原 status / dismissed_*。
                await repo.upsert_suggestion(
                    tenant_id, kb_id, src, tgt, round(ratio, 4), method,
                    reset_dismissed=(src == source_id),
                )
            await session.commit()

    async def scan_document(
        self, tenant_id: str, document_id: str,
    ) -> list[dict] | None:
        """解析完成后：签名 + 全库预筛 + 精确复核，落建议。

        返回 ``None`` 表示**文本尚不可读**（解析刚收口时引擎投影可能还没就绪），
        调用方应短延迟重试；返回 ``[]`` 才是「读到了文本、确实没有命中」。
        """
        async with get_db_session() as session:
            doc = await KnowledgeDocumentRepository(session).get_required(
                document_id, tenant_id,
            )
            kb = await KnowledgeInfoRepository(session).get_required(
                doc.knowledge_base_id, tenant_id,
            )
            if not kb.similarity_check_enabled:
                return []
            rows = (await session.execute(
                select(
                    KnowledgeDocument.id, KnowledgeDocument.created_at,
                ).where(
                    KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.knowledge_base_id == kb.id,
                    KnowledgeDocument.is_deleted == 0,
                    KnowledgeDocument.status == DocStatus.READY.value,
                    KnowledgeDocument.id != doc.id,
                )
            )).all()
            targets_meta = {
                target_id: created_at for target_id, created_at in rows
            }

        # 源文本只读一次：签名与精确复核复用同一份。
        source_text, source, source_ok = await self._load_text(doc)
        if not source_ok or not source_text:
            # 不能当成「无相似」：解析刚收口时可能还查不到文本。
            return None
        if not await self.compute_signature(doc, text=source_text, source=source):
            return []

        cfg = get_config()
        cap = int(getattr(cfg, "DOCUMENT_SIMILARITY_TEXT_CAP", 2_000_000))
        source_text = source_text[:cap]
        top_k = int(cfg.DOCUMENT_SIMILARITY_TOP_K)
        threshold = float(
            kb.similarity_check_threshold or cfg.DOCUMENT_SIMILARITY_THRESHOLD
        )
        # 预筛阈值不能高于最终阈值，否则 KB 阈值调到 0.8 以下时永远命中不到
        cand_threshold = min(
            float(cfg.DOCUMENT_SIMILARITY_CANDIDATE_THRESHOLD),
            max(0.0, threshold - 0.05),
        )
        suppressed = set(
            (doc.extra_metadata or {}).get("similarity_suppressed_targets") or []
        )

        async with get_db_session() as session:
            repo = KnowledgeSimilarityRepository(session)
            src_sig = await repo.get_signature(tenant_id, doc.id)
            if src_sig is None or src_sig.simhash is None:
                return []
            ranked = rank_candidates(
                src_sig.simhash,
                await repo.list_signatures(tenant_id, kb.id),
                valid_ids=set(targets_meta),
                suppressed=suppressed,
                candidate_threshold=cand_threshold,
                top_k=top_k,
            )

        hits: list[tuple[float, str, str]] = []
        load_failed = False
        for _est, target_id in ranked:
            try:
                async with get_db_session() as session:
                    target_doc = await KnowledgeDocumentRepository(
                        session,
                    ).get_required(target_id, tenant_id)
                target_text, _, target_ok = await self._load_text(target_doc)
                if not target_ok:
                    load_failed = True
                    continue
                if not target_text:
                    # 空文本是「无命中」，不是读取失败。
                    continue
                ratio, method = await asyncio.to_thread(
                    DocumentService._text_similarity,
                    source_text, target_text[:cap],
                )
            except Exception:
                logger.warning(
                    "候选文本读取/比对失败: doc=%s target=%s",
                    doc.id, target_id, exc_info=True,
                )
                load_failed = True
                continue
            if ratio >= threshold:
                hits.append((ratio, target_id, method))

        if load_failed:
            # 读取失败不等于「不再相似」：跳过 supersede，可读候选照常更新，
            # 未读到的候选保留原有建议，下一轮再算。
            logger.warning("部分候选文本读取失败，保留既有建议 doc=%s", doc.id)

        hits.sort(reverse=True)
        await self._persist_hits(
            tenant_id, kb.id, doc.id, hits,
            source_created_at=doc.created_at,
            target_created_at=targets_meta,
            preserve_existing=load_failed,
        )
        return [
            {"target_document_id": t, "similarity": round(r, 4), "method": m}
            for r, t, m in hits
        ]

    async def get_suggestions(
        self, tenant_id: str, document_id: str, *, user_id: str | None = None,
    ) -> dict:
        """本文档（较新文件）作为 source 的 pending 合并建议。

        `can_merge` 只有双方都 compiled + READY 才为 True；`block_reason`
        只表达**目标侧**未满足的原因（`unready` = status 非 READY，否则为
        `_compile_state` 值），`source_block_reason` 表达本文档侧的原因。
        目标行缺失（跨租户/已删除/跨 KB/超密级）逐条跳过，不整接口 404。
        """
        async with get_db_session() as session:
            doc_repo = KnowledgeDocumentRepository(session)
            doc = await doc_repo.get_required(document_id, tenant_id)
            rows = await KnowledgeSimilarityRepository(
                session,
            ).list_pending_for_document(tenant_id, document_id)
            targets = {
                target.id: target
                for target in await doc_repo.get_by_ids(
                    [row.target_document_id for row in rows], tenant_id,
                )
            }
            kb = await KnowledgeInfoRepository(session).get_required(
                doc.knowledge_base_id, tenant_id,
            )
            if targets:
                # 目标文档超密级：跳过该条（源文档密级已由能力出口守卫拦成 404）。
                from jonex_core.security import classification_access as policy

                from .space_permission_service import is_tenant_admin

                allowed = set(await policy.filter_allowed_documents(
                    session, tenant_id, user_id or "", list(targets),
                    is_tenant_admin=await is_tenant_admin(tenant_id, user_id),
                ))
            else:
                allowed = set()

        kb_type = kb.kb_type or "lightrag"
        if _status_value(doc.status) != DocStatus.READY.value:
            source_block_reason = "unready"
        else:
            source_state = _compile_state(doc, kb_type)
            source_block_reason = (
                None if source_state == "compiled" else source_state
            )
        items = []
        for row in rows:
            target = targets.get(row.target_document_id)
            if target is None or target.id not in allowed:
                continue
            if target.knowledge_base_id != doc.knowledge_base_id:
                # 合并只支持同一 KB：历史脏数据不得出现在建议里。
                continue
            if _status_value(target.status) != DocStatus.READY.value:
                block_reason = "unready"
            else:
                target_state = _compile_state(target, kb_type)
                block_reason = None if target_state == "compiled" else target_state
            items.append({
                **row.to_dict(),
                "target_file_name": target.file_name,
                "can_merge": (
                    block_reason is None and source_block_reason is None
                ),
                "block_reason": block_reason,
                "source_block_reason": source_block_reason,
            })
        return {"items": items}

    async def dismiss_suggestion(
        self,
        tenant_id: str,
        suggestion_id: str,
        document_id: str,
        *,
        user_id: str | None = None,
    ) -> dict:
        """忽略一条 pending 建议；条件更新避免与并发合并互相覆盖。"""
        not_found = translate(
            "err.similarity.suggestion_not_found", fallback="合并建议不存在",
        )
        handled = translate(
            "err.similarity.suggestion_handled", fallback="该建议已被处理",
        )
        async with get_db_session() as session:
            # 先校验文档存在（租户 + 未删）：解析失败时不得跳过写权限/存在性检查。
            doc = await KnowledgeDocumentRepository(session).get_required(
                document_id, tenant_id,
            )
            # KB 已软删但文档行残留时，同样按不存在处理，杜绝写权限缺口。
            await KnowledgeInfoRepository(session).get_required(
                doc.knowledge_base_id, tenant_id,
            )
            repo = KnowledgeSimilarityRepository(session)
            sug = await repo.get_suggestion(tenant_id, suggestion_id)
            if sug is None or sug.source_document_id != document_id:
                # 与 merge 同文案：不区分「不存在」与「不属于该文档」。
                raise ResourceNotFoundError(message=not_found)
            if sug.status != STATUS_PENDING:
                raise ResourceConflictError(message=handled)
            if not await repo.dismiss_if_pending(
                tenant_id, suggestion_id, dismissed_by=user_id,
            ):
                # 预检查后、条件更新前被并发合并/忽略。
                raise ResourceConflictError(message=handled)
            await session.commit()
        return {"dismissed": True}

    async def merge_suggestion(
        self,
        tenant_id: str,
        suggestion_id: str,
        source_document_id: str,
        *,
        user_id: str | None = None,
        username: str | None = None,
        ip: str | None = None,
    ) -> dict:
        """把一条 pending 建议合并为已有文档的新版本。

        - `source_document_id`（后创建的新文档）整体并入 `target_document_id`：
          文件复制到目标独占 key、追加 `source="merge"` 版本行、源文档软删；
        - 合并后目标强制重新解析；reparse 失败不影响已提交的合并，响应带
          `reparse_error` 由调用方提示。
        """
        tenant_id = require_tenant(tenant_id)
        not_found = translate(
            "err.similarity.suggestion_not_found", fallback="合并建议不存在",
        )
        handled = translate(
            "err.similarity.suggestion_handled", fallback="该建议已被处理",
        )

        # ── 校验：全部在第一个 session 内、写任何东西之前 ──
        async with get_db_session() as session:
            sim_repo = KnowledgeSimilarityRepository(session)
            suggestion = await sim_repo.get_suggestion(tenant_id, suggestion_id)
            if (
                suggestion is None
                or suggestion.source_document_id != source_document_id
            ):
                raise ResourceNotFoundError(message=not_found)
            if suggestion.status != STATUS_PENDING:
                raise ResourceConflictError(message=handled)
            if suggestion.source_document_id == suggestion.target_document_id:
                raise ResourceConflictError(message="建议数据异常")

            doc_repo = KnowledgeDocumentRepository(session)
            source = await doc_repo.get_required(source_document_id, tenant_id)
            target = await doc_repo.get_required(
                suggestion.target_document_id, tenant_id,
            )
            # 密级守卫必须在**复制任何对象之前**：超密级沿用 404，不泄露存在性。
            from jonex_core.security import classification_access as policy

            from .space_permission_service import is_tenant_admin

            await policy.assert_documents_allowed(
                session, tenant_id, user_id or "", [source.id, target.id],
                is_tenant_admin=await is_tenant_admin(tenant_id, user_id),
            )
            if source.knowledge_base_id != target.knowledge_base_id:
                raise ResourceConflictError(message="仅支持合并同一知识库内的文档")
            if _status_value(source.status) in (
                DocStatus.ARCHIVED.value, DocStatus.DELETED.value,
            ):
                raise ResourceConflictError(message="源文档已归档或已删除，不能合并")
            if _status_value(source.status) != DocStatus.READY.value:
                raise ResourceConflictError(message="源文档未就绪，不能合并")
            if _status_value(target.status) in (
                DocStatus.ARCHIVED.value, DocStatus.DELETED.value,
            ):
                raise ResourceConflictError(message="目标文档已归档或已删除，不能合并")
            if _status_value(target.status) != DocStatus.READY.value:
                raise ResourceConflictError(message="目标文档未就绪，不能合并")

            task_repo = TaskCenterDocumentRepository(session)
            if (
                await task_repo.exists_active(source.id, tenant_id)
                or await task_repo.exists_active(target.id, tenant_id)
            ):
                raise ResourceConflictError(
                    message="文档正在处理中，请等待当前步骤结束后再合并",
                )

            kb = await KnowledgeInfoRepository(session).get_required(
                source.knowledge_base_id, tenant_id,
            )
            if (
                _compile_state(source, kb.kb_type) != "compiled"
                or _compile_state(target, kb.kb_type) != "compiled"
            ):
                raise ResourceConflictError(message="文档尚未编译完成，不能合并")

            # 提交前快照：源文档软删后按行反查会拿不到，索引清理/对象删除
            # 必须用这里的值。
            target_id = target.id
            target_kb_id = target.knowledge_base_id
            rag_doc_ids = list(source.rag_doc_ids or [])
            kb_id = source.knowledge_base_id
            file_name = source.file_name
            source_key = source.storage_key
            source_backend = (source.storage_backend or "local").strip().lower()
            doc_backend = ingest_backend.backend_for_document(source)
            kb_type = kb.kb_type
            source_file_name = source.file_name
            source_file_size = source.file_size or 0
            source_mime_type = source.mime_type
            source_content_hash = source.content_hash

        # ── 源文档索引 id 解析（参数同删除链）：提交后按快照清理 ──
        doc_service = DocumentService()
        rag_doc_ids = await doc_service._resolve_rag_doc_ids(
            tenant_id=tenant_id,
            document_id=source_document_id,
            kb_id=kb_id,
            file_name=file_name,
            rag_doc_ids=rag_doc_ids,
        )

        # ── 复制源文件到目标独占 key（uuid 前缀避免同名覆盖目标在用对象） ──
        # 复制与第二段事务一起兜底：事务失败时删掉本轮刚写的对象，不留孤儿。
        copied = False
        committed = False
        new_key: str | None = None
        try:
            storage = get_object_storage_for(source_backend)
            raw = await storage.get_bytes(source_key)
            new_key = build_object_key(
                tenant_id, target_kb_id, target_id,
                f"{uuid4().hex}_{source_file_name or 'unnamed'}",
            )
            await storage.put_bytes(
                new_key, raw,
                content_type=source_mime_type or "application/octet-stream",
            )
            copied = True
            file_path = storage.fs_path(new_key) or new_key

            # ── 版本行 + 文档行 + 建议状态：同一事务提交 ──
            async with get_db_session() as session:
                # 行锁重读：并发删除 / 重解析 / 另一次合并必须在这里被挡住。
                source = (await session.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.id == source_document_id,
                        KnowledgeDocument.tenant_id == tenant_id,
                        KnowledgeDocument.is_deleted == 0,
                    ).with_for_update()
                )).scalars().first()
                target = (await session.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.id == target_id,
                        KnowledgeDocument.tenant_id == tenant_id,
                        KnowledgeDocument.is_deleted == 0,
                    ).with_for_update()
                )).scalars().first()
                if source is None:
                    raise ResourceConflictError(message="源文档不存在或已删除，不能合并")
                if target is None:
                    raise ResourceConflictError(message="目标文档不存在或已删除，不能合并")
                if _status_value(source.status) != DocStatus.READY.value:
                    raise ResourceConflictError(message="源文档未就绪，不能合并")
                if _status_value(target.status) != DocStatus.READY.value:
                    raise ResourceConflictError(message="目标文档未就绪，不能合并")
                task_repo = TaskCenterDocumentRepository(session)
                if (
                    await task_repo.exists_active(source_document_id, tenant_id)
                    or await task_repo.exists_active(target_id, tenant_id)
                ):
                    raise ResourceConflictError(
                        message="文档正在处理中，请等待当前步骤结束后再合并",
                    )

                # 并发双击：只有仍处于 pending 的一方能把建议置为 merged。
                sim_repo = KnowledgeSimilarityRepository(session)
                if not await sim_repo.merge_if_pending(tenant_id, suggestion_id):
                    raise ResourceConflictError(message=handled)

                version_repo = KnowledgeDocumentVersionRepository(session)
                version_no = await version_repo.next_version_no(tenant_id, target_id)
                await version_repo.create(
                    KnowledgeDocumentVersion(
                        tenant_id=tenant_id,
                        document_id=target_id,
                        version_no=version_no,
                        file_name=source_file_name,
                        file_size=source_file_size,
                        mime_type=source_mime_type,
                        storage_backend=source_backend,
                        storage_key=new_key,
                        content_hash=source_content_hash,
                        source="merge",
                        created_by=username or user_id,
                    )
                )

                target.file_name = source_file_name
                target.file_size = source_file_size
                target.mime_type = source_mime_type
                target.storage_backend = source_backend
                target.storage_key = new_key
                target.file_path = file_path
                target.content_hash = source_content_hash
                await session.flush()
                trim_objects = await doc_service._trim_document_versions(
                    session, tenant_id, target_id, version_repo, new_key,
                )

                # 源文档版本行里的对象也随文档一起回收。
                source_objects = [
                    ((row.storage_backend or "local").strip().lower(), row.storage_key)
                    for row in await version_repo.list_for_document(
                        tenant_id, source_document_id,
                    )
                    if row.storage_key
                ]
                if source_key:
                    source_objects.insert(0, (source_backend, source_key))

                await KnowledgeDocumentRepository(session).delete_soft(
                    source.id, tenant_id,
                )
                # 合并完成后两端其余 pending 建议不再成立；刚置 merged 的行不受影响。
                await sim_repo.invalidate_for_document(tenant_id, source_document_id)
                await sim_repo.invalidate_for_document(tenant_id, target_id)
                await sim_repo.delete_signature(tenant_id, source_document_id)
                await session.commit()
                committed = True
        except Exception:
            # 提交成功后的异常绝不能删已提交引用的对象：宁可留孤儿。
            if copied and new_key and not committed:
                try:
                    await get_object_storage_for(source_backend).delete(new_key)
                except Exception:
                    logger.warning(
                        "合并失败后清理已复制对象失败（可人工清理）: %s",
                        new_key, exc_info=True,
                    )
            raise

        # ── 提交后的清理与重建：逐步 best-effort，不回滚合并 ──
        try:
            await doc_service._delete_version_objects(trim_objects)
        except Exception:
            logger.warning("合并后清理目标历史版本对象失败", exc_info=True)

        # 索引没清干净就不能打标：否则删库补偿器会当它已处理而跳过重试。
        indexes_ok = False
        try:
            await doc_service._remove_document_indexes(
                tenant_id=tenant_id,
                document_id=source_document_id,
                kb_id=kb_id,
                file_name=file_name,
                rag_doc_ids=rag_doc_ids,
                doc_backend=doc_backend,
                kb_type=kb_type,
            )
            indexes_ok = True
        except Exception:
            logger.warning(
                "合并后清理源文档索引失败 doc=%s",
                source_document_id, exc_info=True,
            )

        if indexes_ok:
            try:
                await doc_service.mark_indexes_cleaned(
                    tenant_id, source_document_id,
                )
            except Exception:
                logger.warning(
                    "合并后标记源文档索引已清理失败 doc=%s",
                    source_document_id, exc_info=True,
                )

        seen_objects: set[tuple[str, str]] = set()
        for backend, key in source_objects:
            if (backend, key) in seen_objects:
                continue
            seen_objects.add((backend, key))
            try:
                await get_object_storage_for(backend).delete(key)
            except Exception:
                logger.warning(
                    "删除已合并源文档对象失败（可人工清理）: %s", key,
                    exc_info=True,
                )

        # 引擎侧 parse.md 由全局存储写入，读取/清理也必须同源。
        try:
            await get_object_storage().delete(
                build_parse_md_key(tenant_id, kb_id, source_document_id)
            )
        except Exception:
            logger.warning(
                "删除已合并源文档 parse.md 失败（可人工清理）: doc=%s",
                source_document_id, exc_info=True,
            )

        reparse: dict = {}
        reparse_error: str | None = None
        try:
            reparse = await doc_service.reparse_document(
                tenant_id, target_id,
                user_id=user_id, username=username, ip=ip,
                force=True, reason="merge_version",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "合并文档版本后重新解析失败 target=%s: %s",
                target_id, exc, exc_info=True,
            )
            reparse_error = getattr(exc, "message", None) or str(exc)

        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": _audit_user_id(user_id),
            "username": username,
            "ip": ip,
            "log_type": "OPERATION",
            "action": "merge_document_version",
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(target_id),
            "request_params": {
                "source_document_id": source_document_id,
                "target_document_id": target_id,
                "version_no": version_no,
                "reparse_ok": reparse_error is None,
            },
        })

        result = {
            "target_document_id": target_id,
            "version_no": version_no,
            "reparse": reparse,
        }
        if reparse_error:
            result["reparse_error"] = reparse_error
        return result

    async def backfill_once(self) -> int:
        """开关开启的 KB：补缺签名文档 + 重试超过 TTL 的 unavailable 哨兵。

        新补到签名的文档随即扫一轮（`scan_document`）：解析收口那次扫描可能因
        引擎投影未就绪被跳过（`scan_document` 返回 ``None`` 且退避耗尽），只补签名
        的话这些文档永远不会有建议。每轮最多 `batch` 篇，扫描内部 top-K 封顶，
        不会退化成全库两两比对。
        """
        cfg = get_config()
        # 批次至少 1 条：<=0 会让每轮都空转，且与「关闭」混淆。
        batch = max(1, int(cfg.DOCUMENT_SIMILARITY_BACKFILL_BATCH))
        retry_minutes = int(cfg.DOCUMENT_SIMILARITY_RETRY_MINUTES)
        # <=0 = 显式关闭 unavailable 重试（不做 TTL 重算，哨兵永久保留）。
        retry_enabled = retry_minutes > 0
        retry_age = retry_minutes * 60
        async with get_db_session() as session:
            kbs = list((await session.execute(
                select(KnowledgeInfo).where(
                    KnowledgeInfo.is_deleted == 0,
                    KnowledgeInfo.similarity_check_enabled.is_(True),
                )
            )).scalars())
        done = 0
        for kb in kbs:
            if not kb.similarity_check_enabled:
                continue
            # 单 KB 失败（查询/连接抖动）不拖垮其余 KB；逐篇失败另在下方单独兜住。
            # CancelledError 是 BaseException，照常上抛。
            try:
                async with get_db_session() as session:
                    repo = KnowledgeSimilarityRepository(session)
                    missing = await repo.list_documents_missing_signature(
                        kb.tenant_id, kb.id, batch,
                    )
                    retry = (
                        await repo.list_documents_retry_unavailable(
                            kb.tenant_id, kb.id, batch, retry_age,
                        )
                        if retry_enabled else []
                    )
                for doc_id in list(dict.fromkeys([*missing, *retry])):
                    # 单篇失败不拖垮整轮。
                    try:
                        async with get_db_session() as session:
                            doc = await KnowledgeDocumentRepository(session).get_by_id(
                                doc_id, kb.tenant_id,
                            )
                        if doc is not None and await self.compute_signature(doc):
                            done += 1
                            try:
                                await self.scan_document(doc.tenant_id, doc.id)
                            except Exception:
                                logger.warning(
                                    "回填后扫描失败 doc=%s", doc.id, exc_info=True,
                                )
                    except Exception:
                        logger.warning("回填单篇失败 doc=%s", doc_id, exc_info=True)
                        continue
            except Exception:
                logger.warning("回填 KB 失败 kb=%s", kb.id, exc_info=True)
                continue
        return done


# READY 收口与引擎投影可见之间有秒级~分钟级延迟（实测在 READY 当下
# 查不到 chunk）。逐次退避重试，问题没解决就交给回填兜底。
_SCAN_RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0, 120.0)


async def schedule_similarity_scan(
    *, tenant_id: str, document_id: str, knowledge_base_id: str,
) -> None:
    """READY 后异步扫描；开关判断在扫描内部，失败只记日志。

    `knowledge_base_id` 当前未使用（扫描内部按文档查询 KB），保留签名。
    文本尚未就绪（`scan_document` 返回 ``None``）时按 `_SCAN_RETRY_DELAYS`
    退避重试——否则解析刚收口那一刻的空投影会被当成「无相似」静默吞掉。
    """

    async def _run() -> None:
        svc = DocumentSimilarityService()
        for delay in (0.0, *_SCAN_RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with _get_scan_sem():
                    result = await svc.scan_document(tenant_id, document_id)
            except Exception:
                logger.warning(
                    "相似检测失败 doc=%s", document_id, exc_info=True,
                )
                return
            if result is not None:
                return
        logger.info(
            "相似检测等待文本超时（交给回填重试） doc=%s", document_id,
        )

    task = asyncio.create_task(_run())
    _pending_scan_tasks.add(task)
    task.add_done_callback(_pending_scan_tasks.discard)


_backfill_task: asyncio.Task | None = None


def schedule_backfill_once() -> None:
    """开关打开后立即跑一次回填；不阻塞请求，失败只记日志。

    单飞：上一轮回填还没跑完时直接返回，避免反复开关把回填堆成一串。
    """

    global _backfill_task
    if _backfill_task is not None and not _backfill_task.done():
        return

    async def _run() -> None:
        try:
            async with _get_scan_sem():
                await DocumentSimilarityService().backfill_once()
        except Exception:
            logger.warning("相似签名回填失败", exc_info=True)

    task = asyncio.create_task(_run())
    _backfill_task = task
    _pending_scan_tasks.add(task)

    def _cleanup(done: asyncio.Task) -> None:
        global _backfill_task
        _pending_scan_tasks.discard(done)
        if _backfill_task is done:
            _backfill_task = None

    task.add_done_callback(_cleanup)


async def drain_pending_similarity_tasks(timeout: float = 5) -> None:
    """关停时尽力等待在途的相似检测/回填任务收尾。

    超时后取消残留任务并记 warning（不能让它们带着已关闭的会话/事件循环跑）。
    """
    pending = list(_pending_scan_tasks)
    if not pending:
        return
    _done, still_pending = await asyncio.wait(pending, timeout=timeout)
    if not still_pending:
        return
    logger.warning(
        "相似检测/回填任务超时未收尾，取消 %d 个", len(still_pending),
    )
    for task in still_pending:
        task.cancel()
    # 取消后的收尸也要有界：卡在不可中断 I/O 的任务可能不响应 cancel，
    # 不能让它把关停无限拖住。
    try:
        await asyncio.wait_for(
            asyncio.gather(*still_pending, return_exceptions=True),
            timeout=2,
        )
    except asyncio.TimeoutError:
        logger.warning("取消相似检测/回填任务后仍未收尾，放弃等待")
