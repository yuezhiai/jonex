#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""领域级数据源连接池服务（同一领域内可复用）。

`DataSourceService` 的平台级孪生：同一套凭据口径（`db_config`）、同一个
SchemaSource，区别只在于主键是 `connection_id` 而不是 `knowledge_base_id`。

**为什么单独一个类而不是塞进 DataSourceService**：这张表的业务规则不一样——
它不直接挂 `knowledge_base_id`（只挂 `space_id`）、删除时要反查引用并**拒绝**（spec §3.4），
而 DataSourceService 的删除是软删自己那一行。
"""
import logging
from uuid import uuid4

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..models.data_source_connection import DataSourceConnection
from ..repository.data_source_connection_repository import DataSourceConnectionRepository
from ..repository.data_source_repository import KnowledgeDataSourceRepository
from .db_config import (
    catalog_source_for,
    encrypt_config,
    reconcile_readonly_mode,
    resolve_database_config,
    strip_client_secret_refs,
)

logger = logging.getLogger(__name__)

# 连接池目前只承载数据库连接。api/storage 那类「文档来源」没有池化的理由：
# 它们本来就该挂在某个知识库下（一个库一份）。
_VALID_CONNECTION_ACCESS_TYPES = {"database"}


class DataSourceConnectionService:
    # ── 读 ──
    async def list_connections(self, tenant_id: str, space_id: str = "",
                               offset: int = 0, limit: int = 200) -> dict:
        tenant_id = require_tenant(tenant_id)
        if not space_id:
            # 没领域就不给列表：返回全租户会让「切换领域」变成只是换了个标签。
            raise InvalidParameterError(message=translate(
                "err.datasource.connection_space_required",
                fallback="数据源连接必须按领域空间查询",
            ))
        async with get_db_session() as session:
            items, total = await DataSourceConnectionRepository(session).list_by_space(
                tenant_id, space_id, offset=offset, limit=limit)
        return {"items": [i.to_dict() for i in items], "total": total}

    @staticmethod
    def _assert_space(conn, space_id: str | None) -> None:
        """连接必须属于请求的领域；不匹配按「不存在」处理（1002）。
        用 404 而不是 403：避免跨领域探测连接 id 是否存在。"""
        if space_id and (conn.space_id or "") != space_id:
            raise ResourceNotFoundError(message=translate(
                "err.datasource.connection_not_in_space",
                fallback="该数据源连接不存在或不属于当前领域",
            ))

    async def get_connection(self, tenant_id: str, conn_id: str,
                             space_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            conn = await DataSourceConnectionRepository(session).get_required(conn_id, tenant_id)
            self._assert_space(conn, space_id)
            return conn.to_dict()

    # ── 写 ──
    async def create_connection(self, tenant_id: str, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        space_id = str(data.get("space_id") or "").strip()
        if not space_id:
            raise InvalidParameterError(message=translate(
                "err.datasource.connection_space_required",
                fallback="数据源连接必须绑定领域空间",
            ))
        access_type = data.get("access_type") or "database"
        if access_type not in _VALID_CONNECTION_ACCESS_TYPES:
            raise InvalidParameterError(message=translate(
                "err.datasource.invalid_connection_access_type",
                params={"access_type": access_type},
                fallback=f"连接池不支持的类型: {access_type}",
            ))
        # 客户端带来的引用键一律丢（见 `db_config.strip_client_secret_refs`）：新建
        # 表单里那份通常是「拿 GET 的响应当模板」抄回来的，存进去就是一条假密文。
        cfg = encrypt_config(access_type, strip_client_secret_refs(data.get("config_json")))
        # 「主账号即只读」与显式只读凭据互斥：置真时清掉后者，避免过期口令以后复活
        cfg = reconcile_readonly_mode(cfg, cfg, access_type)
        # 引擎认不认在这里就判掉，别等用户点「测连接」才发现
        catalog_source_for(cfg.get("engine"))

        async with get_db_session() as session:
            created = await DataSourceConnectionRepository(session).create(DataSourceConnection(
                id=str(uuid4()),
                tenant_id=tenant_id,
                name=data["name"],
                access_type=access_type,
                config_json=cfg,
                status="active",
                space_id=space_id,
            ))
            result = created.to_dict()
            await session.commit()
        return result

    async def update_connection(self, tenant_id: str, conn_id: str, data: dict,
                                space_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = DataSourceConnectionRepository(session)
            conn = await repo.get_required(conn_id, tenant_id)
            self._assert_space(conn, space_id)
            if data.get("name") is not None:
                conn.name = data["name"]
            if data.get("status") is not None:
                conn.status = data["status"]
            if data.get("config_json") is not None:
                # 回传的引用键一律丢（见 `db_config.strip_client_secret_refs`）：
                # 编辑弹窗会把整份 config 抱回来，里面的 `password_ref` 只可能是
                # GET 出口那层脱敏留下的值，不是新密码。改密码走下面的明文 `password`。
                incoming_cfg = strip_client_secret_refs(data["config_json"])
                incoming = encrypt_config(conn.access_type, incoming_cfg)
                # 合并：传了新的明文密码 → incoming 带新的 password_ref（覆盖旧的）；
                # 没传密码 → merged 里保留原 password_ref。
                # 这个「不传即保留」是改密码之外所有编辑（改个 host、改个库名）不丢密码的前提。
                merged = {**(conn.config_json or {}), **incoming}
                # 必须在 merge 之后收敛：否则存量那份只读凭据会从 stored 漏回来
                merged = reconcile_readonly_mode(merged, incoming, conn.access_type)
                catalog_source_for(merged.get("engine"))
                conn.config_json = merged
            await session.flush()
            result = conn.to_dict()
            await session.commit()
        return result

    async def delete_connection(self, tenant_id: str, conn_id: str,
                                space_id: str | None = None) -> dict:
        """软删。**有活跃引用时拒绝**（spec §3.4）。

        静默级联删会让人在某天发现某个库的数据源凭空消失；而拒绝时把库名列出来，
        用户知道该去哪里解绑。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = DataSourceConnectionRepository(session)
            conn = await repo.get_required(conn_id, tenant_id)
            self._assert_space(conn, space_id)
            kb_names = await KnowledgeDataSourceRepository(session).list_referencing_kbs(
                tenant_id, conn_id
            )
            if kb_names:
                raise InvalidParameterError(message=translate(
                    "err.datasource.connection_in_use",
                    params={"count": str(len(kb_names)), "names": "、".join(kb_names)},
                    fallback=(
                        f"该连接仍被 {len(kb_names)} 个知识库引用（{'、'.join(kb_names)}），"
                        f"请先在这些知识库里删除对应数据源"
                    ),
                ))
            await repo.delete_soft(conn, tenant_id)
            await session.commit()
        logger.info("删除数据源连接: conn=%s tenant=%s", conn_id, tenant_id)
        return {"deleted": True}

    # ── 测连接 / 读 catalog ──
    async def _catalog_source(self, tenant_id: str, conn_id: str,
                              space_id: str | None = None):
        async with get_db_session() as session:
            conn = await DataSourceConnectionRepository(session).get_required(conn_id, tenant_id)
            self._assert_space(conn, space_id)
        cfg = resolve_database_config(conn.config_json or {})
        return conn, catalog_source_for(cfg.get("engine")), cfg

    async def test_connection(self, tenant_id: str, conn_id: str,
                              space_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        _, src, cfg = await self._catalog_source(tenant_id, conn_id, space_id)
        return await src.test_connection(cfg)

    async def read_connection_catalog(self, tenant_id: str, conn_id: str,
                                      space_id: str | None = None) -> dict:
        """读库表结构（**只读 information_schema，不取业务数据**）。"""
        tenant_id = require_tenant(tenant_id)
        conn, src, cfg = await self._catalog_source(tenant_id, conn_id, space_id)
        catalog = await src.read_catalog(cfg)
        return {"connection_id": conn.id, **catalog.to_dict()}


__all__ = ["DataSourceConnectionService"]
