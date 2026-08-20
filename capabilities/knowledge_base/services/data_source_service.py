#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""数据源应用服务（CRUD / 测试连接 / 立即同步 / 入站推送）。"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import text

from jonex_core.common.crypto import (
    decode_ingest_key,
    encrypt_secret,
    generate_ingest_key,
    hash_ingest_key,
    verify_ingest_key,
)
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidApiKeyError,
    InvalidParameterError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.object_storage import get_object_storage
from jonex_core.common.tenant import require_tenant

from ..models.data_source import KnowledgeDataSource
from ..repository.data_source_repository import KnowledgeDataSourceRepository
from ..repository.document_repository import KnowledgeDocumentRepository
from .document_service import DocumentService
from .ingestion import get_ingestion_adapter

logger = logging.getLogger(__name__)

_VALID_ACCESS_TYPES = {"api", "api_push", "storage", "file"}


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name or "unnamed")


class DataSourceService:
    def __init__(self) -> None:
        self._docs = DocumentService()

    # ── 内部：凭据加密 ──
    def _encrypt_config(self, access_type: str, cfg: dict) -> dict:
        cfg = dict(cfg or {})
        if access_type == "api":
            auth = dict(cfg.get("auth") or {})
            if auth.get("token"):  # 明文 → 密文引用
                auth["token_ref"] = encrypt_secret(auth.pop("token"))
            cfg["auth"] = auth
        elif access_type == "storage":
            if cfg.get("credential"):  # "ak:sk" 明文 → 密文
                cfg["credential_ref"] = encrypt_secret(cfg.pop("credential"))
        return cfg

    # ── CRUD ──
    async def list_sources(self, tenant_id: str, kb_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDataSourceRepository(session)
            items = await repo.list_by_kb(tenant_id, kb_id)
            total = await repo.count_by_kb(tenant_id, kb_id)
            # document_count 按文档表的 data_source_type 实时统计（按来源方式口径）
            type_count = await KnowledgeDocumentRepository(session).count_by_source_type(tenant_id, kb_id)
        result_items = []
        for i in items:
            d = i.to_dict()
            d["document_count"] = type_count.get(i.access_type, 0)
            result_items.append(d)
        return {"items": result_items, "total": total}

    async def get_source(self, tenant_id: str, ds_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDataSourceRepository(session)
            ds = await repo.get_required(ds_id, tenant_id)
            type_count = await KnowledgeDocumentRepository(session).count_by_source_type(
                tenant_id, ds.knowledge_base_id
            )
            data = ds.to_dict()
            data["document_count"] = type_count.get(ds.access_type, 0)
            return data

    async def create_source(self, tenant_id: str, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        access_type = data.get("access_type")
        if access_type not in _VALID_ACCESS_TYPES:
            raise InvalidParameterError(message=translate("err.datasource.invalid_access_type", params={"access_type": access_type}, fallback=f"非法 access_type: {access_type}"))
        cfg = self._encrypt_config(access_type, data.get("config_json") or {})

        ds_id = str(uuid4())
        kb_id = data["knowledge_base_id"]
        plain: Optional[str] = None
        if access_type == "api_push":
            plain = generate_ingest_key(tenant_id=tenant_id, kb_id=kb_id, ds_id=ds_id)
            cfg["ingest_key_hash"] = hash_ingest_key(plain)
            cfg.setdefault("allowed_ext", ["pdf","doc","docx","ppt","pptx","xls","xlsx","txt","md","html","htm","xhtml","jpg","jpeg","png","gif","bmp","tiff","tif","webp","mp3","wav","flac","aac","m4a","ogg","wma","opus","amr","mp4","avi","mov","mkv","flv","wmv","webm","m4v","mpg","mpeg","3gp"])
            cfg.setdefault("max_file_mb", 50)

        async with get_db_session() as session:
            repo = KnowledgeDataSourceRepository(session)
            ds = await repo.create(
                KnowledgeDataSource(
                    id=ds_id,
                    tenant_id=tenant_id,
                    knowledge_base_id=kb_id,
                    access_method_id=data.get("access_method_id"),
                    access_type=access_type,
                    name=data["name"],
                    config_json=cfg,
                    sync_mode=data.get("sync_mode", "manual"),
                    cron_expr=data.get("cron_expr"),
                    status="active",
                )
            )
            ds_id = ds.id
            result = ds.to_dict()
            await session.commit()

        await self._recalc_kb_stats(tenant_id, kb_id, ds_id)
        if access_type == "api_push" and plain:
            result["ingest_key"] = plain  # 一次性返回明文
            result["ingest_url"] = self._ingest_url(ds_id)
        return result

    async def update_source(self, tenant_id: str, ds_id: str, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDataSourceRepository(session)
            ds = await repo.get_required(ds_id, tenant_id)
            if data.get("name") is not None:
                ds.name = data["name"]
            if data.get("sync_mode") is not None:
                ds.sync_mode = data["sync_mode"]
            if data.get("cron_expr") is not None:
                ds.cron_expr = data["cron_expr"]
            if data.get("status") is not None:
                ds.status = data["status"]
            if data.get("config_json") is not None:
                merged = {**(ds.config_json or {}), **self._encrypt_config(ds.access_type, data["config_json"])}
                ds.config_json = merged
            await session.flush()
            result = ds.to_dict()
            await session.commit()
        return result

    async def delete_source(self, tenant_id: str, ds_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        kb_id: str = ""
        async with get_db_session() as session:
            repo = KnowledgeDataSourceRepository(session)
            ds = await repo.get_required(ds_id, tenant_id)
            kb_id = ds.knowledge_base_id
            await repo.delete_soft(ds, tenant_id)
            await session.commit()
        if kb_id:
            await self._recalc_kb_stats(tenant_id, kb_id, ds_id)
        return {"deleted": True}

    # ── 测试连接 ──
    async def test_source(self, tenant_id: str, ds_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        ds = await self._load(tenant_id, ds_id)
        if ds.access_type == "api_push":
            return {
                "ok": True,
                "message": translate(
                    "success.datasource.push_no_test",
                    fallback="API 推送数据源无需测试连接",
                ),
            }
        adapter = get_ingestion_adapter(ds.access_type)
        return await adapter.test_connection(ds.config_json or {})

    # ── 立即同步（出站 api / storage）──
    async def sync_source(self, tenant_id: str, ds_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        ds = await self._load(tenant_id, ds_id)
        if ds.access_type not in {"api", "storage"}:
            raise InvalidParameterError(message=translate("err.datasource.sync_unsupported", params={"access_type": ds.access_type}, fallback=f"{ds.access_type} 不支持立即同步"))

        await self._set_sync_status(tenant_id, ds_id, "running", None)
        adapter = get_ingestion_adapter(ds.access_type)
        cfg = ds.config_json or {}
        kb_id = ds.knowledge_base_id
        access_type = ds.access_type
        created, failed, errors = 0, 0, []
        try:
            # 手动「立即同步」全量列举，去重交给 _is_ingested（按 external_id）。
            # 不用 last_sync_at 做增量水位：失败/重试会推进水位，导致更早的文件被静默跳过。
            files = await adapter.list_remote_files(cfg, since=None)
            for rf in files:
                try:
                    if await self._is_ingested(tenant_id, kb_id, ds_id, rf.external_id):
                        continue
                    data, mime = await adapter.fetch_bytes(cfg, rf)
                    await self._land_and_ingest(
                        tenant_id, kb_id, ds_id, access_type, rf.name, data, mime,
                        external_id=rf.external_id,
                    )
                    created += 1
                except Exception as fe:  # noqa: BLE001
                    failed += 1
                    errors.append(f"{rf.name}: {fe}")
                    logger.warning("数据源同步单文件失败 ds=%s file=%s err=%s", ds_id, rf.name, fe)
            status = "success" if created or not failed else "failed"
            msg = "; ".join(errors[:5]) if errors else None
            await self._bump_after_sync(tenant_id, ds_id, ds.knowledge_base_id, status, msg)
            return {"created": created, "failed": failed, "message": msg}
        except Exception as e:  # noqa: BLE001
            await self._set_sync_status(tenant_id, ds_id, "failed", str(e))
            raise

    # ── 重置 ingest key（api_push）──
    async def reset_ingest_key(self, tenant_id: str, ds_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDataSourceRepository(session)
            ds = await repo.get_required(ds_id, tenant_id)
            if ds.access_type != "api_push":
                raise InvalidParameterError(message=translate("err.datasource.reset_key_unsupported", fallback="仅 API 开放(推送)数据源支持重置 Key"))
            plain = generate_ingest_key(tenant_id=tenant_id, kb_id=ds.knowledge_base_id, ds_id=ds_id)
            cfg = dict(ds.config_json or {})
            cfg["ingest_key_hash"] = hash_ingest_key(plain)
            ds.config_json = cfg
            await session.flush()
            result = ds.to_dict()
            await session.commit()
        result["ingest_key"] = plain
        result["ingest_url"] = self._ingest_url(ds_id)
        return result

    # ── 入站推送（api_push，内部 action，tenant 由 ds 推导）──
    async def ingest_push(
        self, ds_id: str, ingest_key: str, *,
        storage_key: str, file_name: str, mime_type: Optional[str] = None,
        file_size: int = 0, external_id: Optional[str] = None,
    ) -> dict:
        # 从 key 中提前解析租户/知识库，做自校验（防 ds_id 与 key 不匹配）
        key_info = decode_ingest_key(ingest_key)
        if key_info and key_info.get("ds_id") and key_info["ds_id"] != ds_id:
            raise InvalidApiKeyError(message=translate("err.ingest.key_mismatch", fallback="ingest key 与数据源不匹配"))

        async with get_db_session() as session:
            ds = await session.get(KnowledgeDataSource, ds_id)
            if ds is None or ds.is_deleted or ds.access_type != "api_push":
                raise ResourceNotFoundError(message=translate("err.datasource.not_found", fallback="数据源不存在"))
            tenant_id = ds.tenant_id
            kb_id = ds.knowledge_base_id
            cfg = dict(ds.config_json or {})

            # 用 key 中的租户信息做二次校验
            if key_info:
                if key_info.get("tenant_id") and key_info["tenant_id"] != tenant_id:
                    raise InvalidApiKeyError(message=translate("err.ingest.key_tenant_mismatch", fallback="ingest key 租户不匹配"))
                if key_info.get("kb_id") and key_info["kb_id"] != kb_id:
                    raise InvalidApiKeyError(message=translate("err.ingest.key_kb_mismatch", fallback="ingest key 知识库不匹配"))

        if not verify_ingest_key(ingest_key, cfg.get("ingest_key_hash", "")):
            raise InvalidApiKeyError(message=translate("err.ingest.key_invalid", fallback="ingest key 无效"))
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        allow = {e.lower() for e in (cfg.get("allowed_ext") or [])}
        if allow and ext not in allow:
            raise InvalidParameterError(message=translate("err.datasource.invalid_file_type", params={"ext": ext}, fallback=f"不允许的文件类型: {ext}"))
        max_mb = cfg.get("max_file_mb", 50)
        if file_size and file_size > max_mb * 1024 * 1024:
            raise InvalidParameterError(message=translate("err.datasource.file_too_large", params={"max_mb": str(max_mb)}, fallback=f"文件超过 {max_mb}MB 限制"))

        doc = await self._docs.upload_document(tenant_id, {
            "file_name": file_name,
            "file_path": storage_key,
            "file_size": file_size,
            "mime_type": mime_type,
            "knowledge_base_id": kb_id,
            "storage_backend": os.getenv("OBJECT_STORAGE_BACKEND", "local"),
            "storage_key": storage_key,
            "metadata": {"data_source_id": ds_id, "source": "api_push", "external_id": external_id},
        })
        await self._bump_after_sync(tenant_id, ds_id, kb_id, "success", None)
        return {"document_id": doc.get("id"), "status": doc.get("status")}

    # ── helpers ──
    async def _load(self, tenant_id: str, ds_id: str) -> KnowledgeDataSource:
        async with get_db_session() as session:
            repo = KnowledgeDataSourceRepository(session)
            return await repo.get_required(ds_id, tenant_id)

    def _ingest_url(self, ds_id: str) -> str:
        base = os.getenv("PUBLIC_API_BASE", "").rstrip("/")
        return f"{base}/api/v1/knowledge-base/ingest/{ds_id}"

    async def _land_and_ingest(
        self, tenant_id, kb_id, ds_id, access_type, file_name, data, mime, *, external_id,
    ):
        doc_id = str(uuid4())
        prefix = os.getenv("COS_KEY_PREFIX", "jonex")
        storage_key = f"{prefix}/kb/{tenant_id}/{kb_id}/{doc_id}/{doc_id}_{_safe_name(file_name)}"
        await get_object_storage().put_bytes(storage_key, data, content_type=mime)
        await self._docs.upload_document(tenant_id, {
            "file_name": file_name,
            "file_path": storage_key,
            "file_size": len(data),
            "mime_type": mime,
            "knowledge_base_id": kb_id,
            "doc_id": doc_id,
            "storage_backend": os.getenv("OBJECT_STORAGE_BACKEND", "local"),
            "storage_key": storage_key,
            "metadata": {"data_source_id": ds_id, "source": access_type, "external_id": external_id},
        })

    async def _is_ingested(self, tenant_id, kb_id, ds_id, external_id) -> bool:
        # 按 external_id 去重，不过滤 is_deleted：已同步过（含已删除）的不再重复入库，
        # 既避免重复，也尊重用户删除（不会被下次同步重新拉回）。
        async with get_db_session() as session:
            row = (await session.execute(text(
                """
                SELECT 1 FROM knowledge_base.knowledge_documents
                WHERE tenant_id=:t AND knowledge_base_id=:kb
                  AND extra_metadata->>'data_source_id' = :ds
                  AND extra_metadata->>'external_id' = :ext
                LIMIT 1
                """
            ), {"t": tenant_id, "kb": kb_id, "ds": ds_id, "ext": external_id or ""})).first()
        return row is not None

    async def _recalc_kb_stats(self, tenant_id: str, kb_id: str, ds_id: str) -> None:
        """从 knowledge_documents 表实时统计，校准 knowledge_data_sources.document_count
        和 knowledge_info.document_count / data_source_types。"""
        async with get_db_session() as session:
            # 1. 数据源维度的文档数（从 knowledge_documents 的 metadata->>'data_source_id' 计数）
            await session.execute(text("""
                UPDATE knowledge_base.knowledge_data_sources ds
                   SET document_count = sub.cnt
                  FROM (
                    SELECT d.extra_metadata->>'data_source_id' AS ds_id,
                           COUNT(*) AS cnt
                      FROM knowledge_base.knowledge_documents d
                     WHERE d.is_deleted = 0
                       AND d.tenant_id = :t
                       AND d.extra_metadata->>'data_source_id' IS NOT NULL
                     GROUP BY d.extra_metadata->>'data_source_id'
                  ) sub
                 WHERE ds.id = sub.ds_id
                   AND ds.tenant_id = :t
            """), {"t": tenant_id})

            # 2. 知识库维度的文档数和活跃数据源类型
            await session.execute(text("""
                UPDATE knowledge_base.knowledge_info ki
                   SET document_count = (
                         SELECT COUNT(*)
                           FROM knowledge_base.knowledge_documents d
                          WHERE d.knowledge_base_id = ki.id
                            AND d.is_deleted = 0
                            AND d.tenant_id = ki.tenant_id
                       ),
                       data_source_types = (
                         SELECT COALESCE(
                           jsonb_agg(DISTINCT ds.access_type) FILTER (WHERE ds.access_type IS NOT NULL),
                           '[]'::jsonb
                         )
                           FROM knowledge_base.knowledge_data_sources ds
                          WHERE ds.knowledge_base_id = ki.id
                            AND ds.is_deleted = 0
                            AND ds.status = 'active'
                            AND ds.tenant_id = ki.tenant_id
                       )
                 WHERE ki.id = :kb AND ki.tenant_id = :t
            """), {"kb": kb_id, "t": tenant_id})

            await session.commit()
        logger.debug("_recalc_kb_stats kb=%s ds=%s done", kb_id, ds_id)

    async def _set_sync_status(self, tenant_id, ds_id, status, message):
        async with get_db_session() as session:
            repo = KnowledgeDataSourceRepository(session)
            ds = await repo.get_required(ds_id, tenant_id)
            ds.last_sync_status = status
            ds.last_sync_message = message
            if status == "running":
                ds.last_sync_at = datetime.now(timezone.utc)
            await session.commit()

    async def _bump_after_sync(self, tenant_id, ds_id, kb_id, status, message):
        """同步/推送完成后：更新数据源状态，从文档表重新计数。"""
        async with get_db_session() as session:
            repo = KnowledgeDataSourceRepository(session)
            ds = await repo.get_required(ds_id, tenant_id)
            ds.last_sync_status = status
            ds.last_sync_message = message
            ds.last_sync_at = datetime.now(timezone.utc)
            await session.commit()
        # 不依赖增量，直接从文档表重新计数
        await self._recalc_kb_stats(tenant_id, kb_id, ds_id)


__all__ = ["DataSourceService"]
