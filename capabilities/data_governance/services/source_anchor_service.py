"""W2.6 原文锚点视图（只读聚合）。

结构化资产 → active per-source compiled schema 实体 → 跨模态对齐
（``knowledge_base.ontology_data_alignment``）→ KB 本体实例（doc_ids / attributes）
→ 来源文档列表。锚点口径是**文档 + 实体名/属性**，Neo4j 实体只存 ``doc_ids``、
不存 chunk/page，因此这里**不输出页码**，不伪造原文位置。

失败口径（spec §6）：

- 无 active schema / 无实体 / 无对齐 / 无实例 / 无文档 → ``unresolved=true``；
- 对齐表读取失败（旧库缺表）或 KB 能力调用失败 → ``degraded=true``，单项跳过，
  不抛 500（fail-open），日志 warning；
- ``metric`` / ``document`` / ``data_source`` 没有对齐通道 → ``unresolved=true``。
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import bindparam, text

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    PermissionDeniedError,
)
from jonex_core.common.tenant import require_tenant

from ..asset_identity import split_asset_id
from ..clients.knowledge_base_client import get_knowledge_base_client
from .catalog_service import CatalogService
from .contracts import scaffold
from .permissions import visible_kb_ids

logger = logging.getLogger(__name__)

#: 只有物理表/列能经跨模态对齐找到原文；其余派生资产没有对齐通道。
SUPPORTED_ASSET_TYPES = frozenset({"table", "column"})
KNOWN_ASSET_TYPES = frozenset(
    {"table", "column", "metric", "document", "data_source"}
)

#: 单次实例查询上限（spec §6 与任务口径）。
INSTANCE_PAGE_SIZE = 100


def _text(value) -> str:
    """任意值 → 去空白字符串；None → ''。"""
    if value is None:
        return ""
    return str(value).strip()


def _require_login(user_id) -> None:
    """读视图要求登录用户（与资源视图其余只读接口同口径）。"""
    if not user_id or user_id == "anonymous":
        raise PermissionDeniedError(message="需要登录后查看原文锚点")


def _as_list(value) -> list:
    """Neo4j 列表属性可能是 str（JSON）或 None，统一成 list。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _as_dict(value) -> dict:
    """Neo4j map 属性可能是 str（JSON）或 None，统一成 dict。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    if isinstance(value, dict):
        return value
    return {}


def _merge_attributes(target: dict, attributes: dict) -> None:
    """合并实例属性：同名键保留首个非 None 值（不覆盖先出现的值）。"""
    for key, value in attributes.items():
        name = _text(key)
        if not name or value is None:
            continue
        if name not in target:
            target[name] = value


def _find_entity_name(schema: dict | None, table: str) -> str:
    """在 active compiled schema 中按 ``source_object_id == table`` 找语义实体名。"""
    if not schema:
        return ""
    for entity in schema.get("entity_types") or []:
        if not isinstance(entity, dict):
            continue
        if _text(entity.get("source_object_id")) != table:
            continue
        return _text(entity.get("name"))
    return ""


def _anchor_result(
    asset: dict,
    linked: list[dict],
    *,
    degraded: bool,
) -> dict:
    """按 spec §6 组装返回；``unresolved`` 只由「是否真挂上文档」决定。"""
    resolved = any(item.get("documents") for item in linked)
    return {
        "asset": asset,
        "linked": linked,
        "unresolved": not resolved,
        "degraded": bool(degraded),
    }


class SourceAnchorService:
    """原文锚点视图（table / column）。"""

    # ── 数据装载 ────────────────────────────────────────────────

    async def _load_alignments(
        self, tenant_id: str, data_source_id: str, entity_name: str,
    ) -> list[tuple[str, str]]:
        """读取 active 跨模态对齐，返回去重后的 ``[(kb_id, doc_type)]``。

        表缺失/查询异常由调用方捕获并降级（旧库可能没有 011 迁移）。
        """
        sql = (
            "SELECT knowledge_base_id, doc_type "
            "FROM knowledge_base.ontology_data_alignment "
            "WHERE tenant_id = :tenant_id "
            "AND data_source_id = :data_source_id "
            "AND status = 'active' "
            "AND entity_name = :entity_name "
            "ORDER BY knowledge_base_id, doc_type"
        )
        async with get_db_session() as session:
            rows = (
                await session.execute(
                    text(sql),
                    {
                        "tenant_id": tenant_id,
                        "data_source_id": data_source_id,
                        "entity_name": entity_name,
                    },
                )
            ).mappings().all()

        alignments: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            kb_id = _text(row.get("knowledge_base_id"))
            doc_type = _text(row.get("doc_type"))
            if not kb_id or not doc_type:
                continue
            pair = (kb_id, doc_type)
            if pair in seen:
                continue
            seen.add(pair)
            alignments.append(pair)
        return alignments

    async def _collect_links(
        self,
        tenant_id: str,
        entity_name: str,
        alignments: list[tuple[str, str]],
    ) -> tuple[list[dict], bool]:
        """逐个对齐调用 KB 实例接口；失败的对齐项跳过并标记 degraded。

        返回的条目带内部键 ``doc_ids``（聚合后的去重文档 id），文档名补齐后
        由 ``_attach_documents`` 摘掉，不进入对外形状。
        """
        client = get_knowledge_base_client()
        linked: list[dict] = []
        degraded = False

        for kb_id, doc_type in alignments:
            try:
                data = await client.invoke(
                    "list_ontology_instances",
                    {
                        "knowledge_base_id": kb_id,
                        "entity_type": doc_type,
                        "page": 1,
                        "page_size": INSTANCE_PAGE_SIZE,
                    },
                    tenant_id,
                )
            except Exception as exc:  # noqa: BLE001 - fail-open，单项跳过
                degraded = True
                logger.warning(
                    "source anchors: list_ontology_instances failed, skipped "
                    "(tenant=%s kb=%s doc_type=%s err=%s)",
                    tenant_id, kb_id, doc_type, exc,
                )
                continue

            items = []
            total = None
            if isinstance(data, dict):
                items = [
                    item for item in (data.get("items") or [])
                    if isinstance(item, dict)
                ]
                if isinstance(data.get("total"), int):
                    total = data["total"]

            attributes: dict = {}
            doc_ids: list[str] = []
            for item in items:
                _merge_attributes(attributes, _as_dict(item.get("attributes")))
                for doc_id in _as_list(item.get("doc_ids")):
                    value = _text(doc_id)
                    if value and value not in doc_ids:
                        doc_ids.append(value)

            linked.append({
                "knowledge_base_id": kb_id,
                "doc_type": doc_type,
                "entity_name": entity_name,
                "entity_count": total if total is not None else len(items),
                "truncated": bool(total is not None and total > len(items)),
                "attributes": attributes,
                "documents": [],
                "doc_ids": doc_ids,
            })
        return linked, degraded

    async def _load_documents(
        self, tenant_id: str, doc_ids: list[str],
    ) -> dict[str, dict]:
        """按 id 批量读取未删除文档：``{id: {id, name, status}}``。"""
        if not doc_ids:
            return {}
        sql = (
            "SELECT id, file_name, status "
            "FROM knowledge_base.knowledge_documents "
            "WHERE tenant_id = :tenant_id "
            "AND id IN :doc_ids "
            "AND is_deleted = 0"
        )
        statement = text(sql).bindparams(bindparam("doc_ids", expanding=True))
        async with get_db_session() as session:
            rows = (
                await session.execute(
                    statement,
                    {"tenant_id": tenant_id, "doc_ids": list(doc_ids)},
                )
            ).mappings().all()

        documents: dict[str, dict] = {}
        for row in rows:
            doc_id = _text(row.get("id"))
            if not doc_id:
                continue
            documents[doc_id] = {
                "id": doc_id,
                "name": _text(row.get("file_name")),
                "status": _text(row.get("status")),
            }
        return documents

    def _attach_documents(
        self, linked: list[dict], documents: dict[str, dict],
    ) -> None:
        """把文档行挂到各对齐项并按 doc_ids 顺序去重（缺失的文档静默省略）。"""
        for entry in linked:
            doc_ids = entry.pop("doc_ids", [])
            seen: set[str] = set()
            attached: list[dict] = []
            for doc_id in doc_ids:
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                document = documents.get(doc_id)
                if document is not None:
                    attached.append(dict(document))
            entry["documents"] = attached

    # ── 对外接口 ────────────────────────────────────────────────

    async def get_anchors(
        self, tenant_id: str, user_id, asset_type, asset_id: str,
    ) -> dict:
        """返回资产关联的文档/实体锚点（spec §6 形状）。

        非 ``table`` / ``column`` 一律返回 ``unresolved=true``（无对齐通道），
        不做 DB/KB 调用。
        """
        tenant_id = require_tenant(tenant_id)
        _require_login(user_id)

        normalized_type = _text(asset_type).lower()
        asset = {
            "asset_type": normalized_type,
            "asset_id": _text(asset_id),
        }
        if normalized_type not in KNOWN_ASSET_TYPES:
            raise InvalidParameterError(message="asset_type 非法")

        # 入口先做资产可见性校验：不存在或当前用户不可见一律 1002（与目录
        # 详情同语义）。否则原文锚点会绕开 KB/空间可见性，读到他人资产
        # 的对齐、实体与文档列表。
        await CatalogService().get_asset(
            tenant_id, user_id, normalized_type, asset["asset_id"],
            include_quality=False,
        )

        if normalized_type not in SUPPORTED_ASSET_TYPES:
            return _anchor_result(asset, [], degraded=False)

        data_source_id, table, _column = split_asset_id(
            normalized_type, asset["asset_id"],
        )

        try:
            async with get_db_session() as session:
                schema = await scaffold.load_active_schema(
                    session,
                    tenant_id=tenant_id,
                    data_source_id=data_source_id,
                )
        except Exception as exc:  # noqa: BLE001 - 与对齐查询同口径 fail-open
            logger.warning(
                "source anchors: compiled schema lookup degraded "
                "(tenant=%s ds=%s err=%s)", tenant_id, data_source_id, exc,
            )
            return _anchor_result(asset, [], degraded=True)
        entity_name = _find_entity_name(schema, table)
        if not entity_name:
            return _anchor_result(asset, [], degraded=False)

        try:
            alignments = await self._load_alignments(
                tenant_id, data_source_id, entity_name,
            )
        except Exception as exc:  # noqa: BLE001 - 旧库缺表等，fail-open
            logger.warning(
                "source anchors: alignment lookup degraded "
                "(tenant=%s ds=%s entity=%s err=%s)",
                tenant_id, data_source_id, entity_name, exc,
            )
            return _anchor_result(asset, [], degraded=True)

        if not alignments:
            return _anchor_result(asset, [], degraded=False)

        # 对齐表是租户级的；普通成员只能看到自己有权限的 KB。管理员
        # （None = 不过滤）保持全量；空列表 ⇒ 无可见对齐 ⇒ unresolved。
        visible = await visible_kb_ids(tenant_id, user_id)
        if visible is not None:
            allowed = set(visible)
            alignments = [
                pair for pair in alignments if pair[0] in allowed
            ]
            if not alignments:
                return _anchor_result(asset, [], degraded=False)

        linked, degraded = await self._collect_links(
            tenant_id, entity_name, alignments,
        )

        all_doc_ids: list[str] = []
        for entry in linked:
            for doc_id in entry.get("doc_ids") or []:
                if doc_id not in all_doc_ids:
                    all_doc_ids.append(doc_id)

        documents: dict[str, dict] = {}
        if all_doc_ids:
            try:
                documents = await self._load_documents(tenant_id, all_doc_ids)
            except Exception as exc:  # noqa: BLE001 - 文档名富化失败不影响锚点
                degraded = True
                logger.warning(
                    "source anchors: document lookup degraded "
                    "(tenant=%s docs=%s err=%s)",
                    tenant_id, len(all_doc_ids), exc,
                )

        self._attach_documents(linked, documents)
        return _anchor_result(asset, linked, degraded=degraded)
