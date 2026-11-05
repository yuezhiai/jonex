#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""数据源应用服务（CRUD / 测试连接 / 立即同步 / 入站推送）。"""
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import text

# ⚠️ 必须是 Settings **实例**（`from ...config import config`），不是 config 子模块：
# `LINEAGE_IMPORT_MAX_ROWS` 只定义在 Settings 实例上（同 nl2sql/multiturn.py 的注解）。
from jonex_core.common.config import config
from jonex_core.common.config import get_config
from jonex_core.common.crypto import (
    decode_ingest_key,
    hash_ingest_key,
    generate_ingest_key,
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
from jonex_core.security.outbound_url_validator import validate_kb_ingestion_url

from ..models.data_source import KnowledgeDataSource, mask_secrets
from ..models.data_source_connection import DataSourceConnection
from ..repository.data_source_repository import KnowledgeDataSourceRepository
from ..repository.data_source_connection_repository import DataSourceConnectionRepository
from ..repository.document_repository import KnowledgeDocumentRepository
from .db_config import (
    catalog_source_for,
    encrypt_config,
    merge_config_preserving_secret_refs,
    reconcile_readonly_mode,
    resolve_database_config,
    resolve_readonly_database_config,
    strip_client_secret_refs,
)
from .document_service import DocumentService
from .evidence_probe import evidence_key, probe_source
from .ingestion import get_ingestion_adapter
from .ontology_data_projection import (
    OntologyDataProjectionService,
    project_source_best_effort,
    project_source_delete_best_effort,
    rebuild_alignments_best_effort,
)
from . import schema_gen_backend
from .relation_merge import merge_declared_fks
from .schema_source.base import SchemaSource  # 仅用于类型标注
from .ingestion.api_adapter import validate_api_config

logger = logging.getLogger(__name__)

# database 是**结构来源**而非文档来源：它只读 catalog 出 schema，
# 不走 list_remote_files / fetch_bytes 那条文档链路
_VALID_ACCESS_TYPES = {"api", "api_push", "storage", "file", "database", "etl"}

_ETL_KINDS = {"file", "openlineage", "openmetadata", "datahub"}


def normalize_etl_config(cfg_raw: dict) -> dict:
    """ETL 来源的 config 归一（纯函数，便于单测）。

    - `data_source_id` 必填：血缘边按数据源收口，ETL 来源必须挂到一个已登记的
      database 数据源；
    - `etl_kind` 只许 file / openlineage / openmetadata / datahub；
    - `namespace` 仅 openlineage 用（可选，事件 dataset namespace 白名单）。
      OpenMetadata / DataHub 是**平台推送**，消息里自带表全名，不需要 namespace。
    """
    cfg = dict(cfg_raw or {})
    ds_id = str(cfg.get("data_source_id") or "").strip()
    if not ds_id:
        raise InvalidParameterError(message=translate(
            "err.datasource.etl_data_source_required",
            fallback="ETL 来源必须指定 data_source_id"))
    kind = str(cfg.get("etl_kind") or "").strip()
    if kind not in _ETL_KINDS:
        raise InvalidParameterError(message=translate(
            "err.datasource.etl_kind_invalid", params={"kind": kind},
            fallback=f"非法 etl_kind: {kind}（只许 file / openlineage / openmetadata / datahub）"))
    out = {"data_source_id": ds_id, "etl_kind": kind}
    if kind == "openlineage":
        ns = str(cfg.get("namespace") or "").strip()
        if ns:
            out["namespace"] = ns
    return out


def _validate_etl_target(kb_id: str, target_kb_id: Any, target_access_type: Any) -> None:
    """ETL 目标必须是**同一知识库**里已登记的 database 数据源。

    建与改共用这一处：`update_source` 也能改 `config_json`，只在 create 校验
    等于给 update 留了一条旁路（把目标改绑到别的库 / 非 database 源）。
    """
    if target_kb_id != kb_id:
        raise InvalidParameterError(message=translate(
            "err.datasource.etl_target_kb_mismatch",
            params={"target_kb": target_kb_id, "kb": kb_id},
            fallback=(f"ETL 目标数据源属于知识库 {target_kb_id}，"
                      f"不是当前知识库 {kb_id}"),
        ))
    if target_access_type != "database":
        raise InvalidParameterError(message=translate(
            "err.datasource.etl_target_not_database",
            params={"access_type": target_access_type},
            fallback="ETL 目标必须是 database 类型的数据源",
        ))


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name or "unnamed")


def _is_valid_http_url(value: Any) -> bool:
    """校验 endpoint 必须是 http/https URL（与前端 pattern 对齐）。"""
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value:
        return False
    try:
        parts = urlparse(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _connection_public_view(conn: DataSourceConnection) -> dict:
    """连接对外可见的那份配置（脱敏 + 两个来源标记）。

    读路径（`list_sources` / `get_source` 的回填）与写路径（`update_source`
    判「这次到底改没改配置」）**必须共用同一份口径** —— 两边分叉就会出现
    「前端原样回传却被判成改过，改个名字都改不了」。
    """
    return {**conn.public_config(), "connection_name": conn.name, "connection_id": conn.id}


def _schema_tables_for_probe(entity_types: list) -> list[str]:
    """编译出来的实体 → 该探测哪些物理表。

    **回退是必须的**（C1 的教训，`9b2afc30`）：`source_object_id` 缺失时 ② 那边就是把**实体名**当表名传下去的，
    探测这一侧不跟上，那一档源会**一条证据都探不到**（`probe_source` 会把空表名跳过）。
    ⚠️ 实测 2026-09-30 四个源的编译 schema 都已 100% 带 `source_object_id`，所以回退今天不触发 ——
    但它零成本，且挡住「没重生成过的源」那一档。

    键的算法**只有 `evidence_key` 一处**（写入方 = 这里，读取方 = `nl2sql/service.py`：
    两边各写一遍就会出现「写进实体名那一档、读却去查 `"  "`」的静默取不到）。
    """
    out: list[str] = []
    for entity in entity_types or []:
        # ⚠️ `evidence_key` 里是**先 strip、再回退**：`source_object_id` 是空白串时它在 `or`
        # 眼里照样是真值，那种「先 or 再 strip」的写法会把它 strip 成空、把这条实体整个丢掉 ——
        # 而空白 id 正是回退要救的那一档（等价于「没有物理表名」）。
        name = evidence_key(entity)
        if name:
            out.append(name)
    return out


class DataSourceService:
    def __init__(self) -> None:
        self._docs = DocumentService()

    # ── 内部：endpoint 校验 ──
    def _validate_endpoint(self, access_type: str, cfg: dict, *, require: bool = False) -> None:
        """校验 config_json.endpoint 为合法 http/https URL 并做 SSRF 网段判定。

        - api 出站：endpoint 必填（create 时 require=True）；另校验 method 白名单与
          auth.header_name（见 ``validate_api_config``）；
        - storage 出站：endpoint 可选（AWS S3 走默认端点），填了必须合法。

        ``_is_valid_http_url`` 只做**格式预检**（职责分离），私网 / 回环 / 链路本地 /
        云元数据的网段判定统一交给 ``validate_kb_ingestion_url``（严格档，违规抛
        ``InvalidTargetUrlError``(1007)）；需要拉内网源时由部署方用
        ``KB_INGESTION_ALLOWED_HOSTS`` 白名单显式放行（安全默认 + 逃生阀）。
        """
        if access_type not in ("api", "storage"):
            return
        endpoint = cfg.get("endpoint")
        if not endpoint:
            if require and access_type == "api":
                raise InvalidParameterError(message=translate(
                    "err.datasource.endpoint_required",
                    fallback="请输入 API 接口地址（Endpoint）",
                ))
            return
        if not _is_valid_http_url(endpoint):
            raise InvalidParameterError(message=translate(
                "err.datasource.invalid_endpoint",
                fallback="Endpoint 格式错误，需以 http:// 或 https:// 开头",
            ))
        if access_type == "api":
            validate_api_config(cfg)
        validate_kb_ingestion_url(
            endpoint, allowed_hosts=get_config().kb_ingestion_allowed_hosts
        )

    # ── 内部：引用行回填连接配置 / 写守卫 ──
    async def _connection_configs(
        self, tenant_id: str, rows: list[KnowledgeDataSource],
    ) -> dict[str, dict]:
        """批量取被引用连接的**公开**配置（不含任何凭据），按 connection_id 索引。

        引用行的 config_json 是 `{}` 占位——不回填的话，前端卡片上
        「engine ｜ host:port/database」那一行会退化成 `｜ -:-/-`。
        一次查完，别按行 N+1。
        """
        ids = sorted({r.connection_id for r in rows if r.connection_id})
        if not ids:
            return {}
        async with get_db_session() as session:
            conns = await DataSourceConnectionRepository(session).list_by_ids(tenant_id, ids)
        return {c.id: _connection_public_view(c) for c in conns}

    async def _config_differs_from_connection(self, ds: KnowledgeDataSource, session, incoming: dict) -> bool:
        """入参 config 相对**读路径给前端的那份**有没有真改动（引用行专用）。

        基准 = `list_sources` / `get_source` 回填的那份（连接公开配置 + 本行自己的
        配置，本行那份在合并里靠后、会盖住连接）—— 前端手里拿到的就是它，只有用它
        当基准，「原样回传」才判得出来。

        用调用方手里的 `session`，**不在这里另开**：同一次请求同时占两条池连接，
        池子打满时是自锁（见 `get_source` 里把 return 移出 `async with` 的同款理由）。
        """
        conn = await DataSourceConnectionRepository(session).get_required(ds.connection_id, ds.tenant_id)
        served = {**_connection_public_view(conn), **mask_secrets(ds.config_json or {})}
        return any(served.get(k) != v for k, v in incoming.items())

    # ── CRUD ──
    async def list_sources(self, tenant_id: str, kb_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDataSourceRepository(session)
            items = await repo.list_by_kb(tenant_id, kb_id)
            total = await repo.count_by_kb(tenant_id, kb_id)
            # document_count 按文档表的 data_source_type 实时统计（按来源方式口径）
            type_count = await KnowledgeDocumentRepository(session).count_by_source_type(tenant_id, kb_id)
        conn_cfgs = await self._connection_configs(tenant_id, items)
        result_items = []
        for i in items:
            d = i.to_dict()
            d["document_count"] = type_count.get(i.access_type, 0)
            if i.connection_id and i.connection_id in conn_cfgs:
                # 连接那份在前、本行那份在后：引用行是 {}，合并后等于连接配置
                d["config_json"] = {**conn_cfgs[i.connection_id], **d["config_json"]}
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
        if ds.connection_id:
            conn_cfgs = await self._connection_configs(tenant_id, [ds])
            if ds.connection_id in conn_cfgs:
                data["config_json"] = {**conn_cfgs[ds.connection_id], **data["config_json"]}
        return data

    async def create_source(self, tenant_id: str, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        access_type = data.get("access_type")
        if access_type not in _VALID_ACCESS_TYPES:
            raise InvalidParameterError(message=translate("err.datasource.invalid_access_type", params={"access_type": access_type}, fallback=f"非法 access_type: {access_type}"))
        cfg_raw = data.get("config_json") or {}
        connection_id = data.get("connection_id") or None
        if connection_id:
            # 引用池子里的连接：配置（含凭证）从连接读，本行只留占位。
            # 先把连接存在性 / 类型 / 引擎三道校验走完再落库，别建出一个指向空气的引用。
            if access_type != "database":
                raise InvalidParameterError(message=translate(
                    "err.datasource.connection_requires_database",
                    params={"access_type": access_type},
                    fallback=f"只有 database 类型的数据源可以引用连接，当前类型 {access_type}",
                ))
            conn = await self._load_connection(tenant_id, connection_id)
            # 连接已按领域隔离：跨领域引用必须在建源时拒绝，否则「本领域看不到
            # 这条连接」只挡住了下拉框，挡不住直接拼 connection_id 的请求。
            kb_space = await self._kb_space_id(tenant_id, data["knowledge_base_id"])
            if conn.space_id and kb_space and conn.space_id != kb_space:
                raise InvalidParameterError(message=translate(
                    "err.datasource.connection_space_mismatch",
                    fallback="该连接属于其他领域空间，不能在此知识库中使用",
                ))
            if conn.access_type != "database":
                raise InvalidParameterError(message=translate(
                    "err.datasource.connection_not_database",
                    params={"connection_id": connection_id},
                    fallback=f"连接 {connection_id} 不是数据库连接",
                ))
            catalog_source_for((conn.config_json or {}).get("engine"))
            cfg: dict = {}
        else:
            self._validate_endpoint(access_type, cfg_raw, require=True)
            # 内联分支和连接池共用同一条写入口径：客户端带来的 `*_ref` /
            # `ingest_key_hash` 一律丢（见 `strip_client_secret_refs`）。
            # 存量内联行（api / storage / 老 database）都由这里落库，漏掉这一步
            # 就等于「拿 GET 响应当模板新建」时把回显占位 `"***"` 当成密文存下来。
            cfg = encrypt_config(access_type, strip_client_secret_refs(cfg_raw))
            cfg = reconcile_readonly_mode(cfg, cfg, access_type)

        ds_id = str(uuid4())
        kb_id = data["knowledge_base_id"]
        plain: Optional[str] = None
        if access_type == "api_push":
            plain = generate_ingest_key(tenant_id=tenant_id, kb_id=kb_id, ds_id=ds_id)
            cfg["ingest_key_hash"] = hash_ingest_key(plain)
            cfg.setdefault("allowed_ext", ["pdf","doc","docx","ppt","pptx","xls","xlsx","txt","md","html","htm","xhtml","jpg","jpeg","png","gif","bmp","tiff","tif","webp","mp3","wav","flac","aac","m4a","ogg","wma","opus","amr","mp4","avi","mov","mkv","flv","wmv","webm","m4v","mpg","mpeg","3gp"])
            cfg.setdefault("max_file_mb", 50)

        if access_type == "etl":
            cfg = normalize_etl_config(cfg_raw)
            target = await self.get_source(tenant_id, cfg["data_source_id"])
            _validate_etl_target(
                kb_id, target.get("knowledge_base_id"), target.get("access_type"),
            )
            plain = generate_ingest_key(tenant_id=tenant_id, kb_id=kb_id, ds_id=ds_id)
            cfg["ingest_key_hash"] = hash_ingest_key(plain)

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
                    connection_id=connection_id,
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
        if access_type == "etl" and plain:
            result["ingest_key"] = plain  # 一次性返回明文
            result["ingest_url"] = self._lineage_ingest_url(ds_id)
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
                incoming = data["config_json"] or {}
                # database 引用行的配置真相在连接池：本行的 config_json 只是占位，
                # 连库 / 测试连接 / 生成 schema 读的都是连接那份（`_db_catalog_source`）。
                # 所以在这里「改配置」必然不生效 —— 改的时候像是成功了，真正连的库
                # 一点没变，正是静默无效写入。有改动就明确拒绝，指路到连接池。
                #
                # 但不能见 config_json 就拒：database 的编辑弹窗**没有配置字段**，
                # 改个名字也会把回填的连接配置原样带回来（`buildEditConfig` 原样返回
                # `editingDs.configJson`），一刀切会把改名一起挡掉。
                config_lives_in_connection = (
                    bool(incoming) and bool(ds.connection_id) and ds.access_type == "database"
                )
                if config_lives_in_connection:
                    if await self._config_differs_from_connection(ds, session, incoming):
                        raise InvalidParameterError(message=translate(
                            "err.datasource.connection_config_readonly",
                            fallback="该数据源引用的是平台连接池里的连接，连接配置请在「平台管理 → 数据接入」里修改；此处只能改名称等本库自己的字段",
                        ))
                    # 走到这里 = 一个值都没改（改名请求的回传）：**不落库**。
                    # 写下去只会把连接那份里的脱敏占位 `password_ref: "***"` 和
                    # `connection_name` 抄进占位行，留下一个假密文引用；读路径看见的
                    # 还是连接那份，值一模一样，所以跳过是等价的。
                elif ds.access_type == "etl":
                    # ETL 的 config 是**受约束的**：改绑目标必须走与建时同一道校验，
                    # 否则 update 就是 create 校验的旁路。`normalize_etl_config` 按
                    # 白名单重建，客户端带来的任意键（含 `ingest_key_hash`）都会被丢掉；
                    # 而那条哈希是服务端管的，要把存量行里那份原样带回来 —— 否则
                    # 「改个 etl_kind」就把正在用的 ingest key 弄失效了。
                    normalized = normalize_etl_config(incoming)
                    target = await repo.get_required(normalized["data_source_id"], tenant_id)
                    _validate_etl_target(
                        ds.knowledge_base_id, target.knowledge_base_id, target.access_type,
                    )
                    stored_hash = (ds.config_json or {}).get("ingest_key_hash")
                    if stored_hash:
                        normalized["ingest_key_hash"] = stored_hash
                    ds.config_json = normalized
                else:
                    self._validate_endpoint(ds.access_type, incoming)
                    # 编辑弹窗（`DataSourceEditModal.buildEditConfig`）对 api / storage /
                    # 存量内联 database 都是**把 GET 那份脱敏配置整份抱回来**，里面
                    # `password_ref` / `auth.token_ref` / `credential_ref` 是回显占位。
                    # 先丢客户端带来的引用键再加密：不丢的话，改个名字就把 `"***"`
                    # 写成了新密文 —— 不报错、回显还是 `***`，直到某次测试连接
                    # 报一句说不清的认证失败（spec §3.2 承诺这些存量行不受影响）。
                    # 明文 `password` / `auth.token` 不受影响，`encrypt_config` 照造新引用。
                    incoming_cfg = encrypt_config(
                        ds.access_type, strip_client_secret_refs(incoming))
                    merged = merge_config_preserving_secret_refs(
                        ds.config_json,
                        incoming_cfg,
                        ds.access_type,
                    )
                    # merge 之后收敛「主账号即只读」开关与显式只读凭据的互斥关系
                    ds.config_json = reconcile_readonly_mode(
                        merged, incoming_cfg, ds.access_type)
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
            # 图是派生物：源删了，该源的数据资产子图也要清掉（best-effort）。
            await project_source_delete_best_effort(tenant_id, kb_id, ds_id)
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
        if ds.access_type == "database":
            src, cfg = await self._db_catalog_source(ds)
            return await src.test_connection(cfg)
        adapter = get_ingestion_adapter(ds.access_type)
        return await adapter.test_connection(ds.config_json or {})

    # ── 内部：数据库数据源（结构来源，非文档来源）──

    async def _kb_space_id(self, tenant_id: str, kb_id: str) -> str:
        """知识库所属领域；不存在/无权访问时按 1002 抛。"""
        from ..repository.knowledge_info_repository import KnowledgeInfoRepository
        async with get_db_session() as session:
            kb = await KnowledgeInfoRepository(session).get_required(kb_id, tenant_id)
        return getattr(kb, "space_id", "") or ""

    async def _load_connection(self, tenant_id: str, connection_id: str) -> DataSourceConnection:
        """取平台连接池里的连接；不存在（或跨租户 / 已删）时给可读的 400。"""
        async with get_db_session() as session:
            repo = DataSourceConnectionRepository(session)
            try:
                return await repo.get_required(connection_id, tenant_id)
            except ResourceNotFoundError as exc:
                raise InvalidParameterError(message=translate(
                    "err.datasource.connection_missing",
                    params={"connection_id": connection_id},
                    fallback=f"引用的连接不存在或无权访问：{connection_id}",
                )) from exc

    async def _db_catalog_source(self, ds: KnowledgeDataSource):
        """取数据源的 SchemaSource 实例 + 解好密的配置。

        两条路径（spec §3.3）：
          - `connection_id` 非空 → 从**连接池**读 config。凭证的单一真相源，
            改一次密码，引用它的 N 个知识库一起生效。
          - `connection_id` 为空 → 用该行自己的 `config_json`（存量内联数据源）。

        密码在库里是密文引用（`password_ref`），只在此处解一次，
        绝不出现在任何返回给前端的结构里，也不跨系统传。
        """
        if ds.access_type != "database":
            raise InvalidParameterError(message=translate(
                "err.datasource.not_database",
                params={"access_type": ds.access_type},
                fallback=f"数据源类型 {ds.access_type} 不是数据库数据源",
            ))
        if ds.connection_id:
            conn = await self._load_connection(ds.tenant_id, ds.connection_id)
            cfg = dict(conn.config_json or {})
        else:
            cfg = dict(ds.config_json or {})
        cfg = resolve_database_config(cfg)
        return catalog_source_for(cfg.get("engine")), cfg

    async def query_config(
        self, tenant_id: str, ds_id: str,
    ) -> Optional[tuple[SchemaSource, dict]]:
        """② NL→SQL 用的连接入口：**只读凭据**解好密的配置 + 适配器。

        与 `_db_catalog_source` 同一套两条路径（引用连接 / 存量内联），
        区别只在凭据口径：这里走 `resolve_readonly_database_config`，
        **没登记只读凭据就返回 None** —— 绝不回退到写凭据。实测某源登记的是
        `prod_user` / 生产库，一次回退就是拿写账号跑模型生成的 SQL。
        """
        tenant_id = require_tenant(tenant_id)
        ds = await self._load(tenant_id, ds_id)
        if ds.access_type != "database":
            raise InvalidParameterError(message=translate(
                "err.datasource.not_database",
                params={"access_type": ds.access_type},
                fallback=f"数据源类型 {ds.access_type} 不是数据库数据源",
            ))
        if ds.connection_id:
            conn = await self._load_connection(ds.tenant_id, ds.connection_id)
            raw = dict(conn.config_json or {})
        else:
            raw = dict(ds.config_json or {})
        cfg = resolve_readonly_database_config(raw)
        if cfg is None:
            return None
        return catalog_source_for(cfg.get("engine")), cfg

    async def read_catalog(self, tenant_id: str, ds_id: str) -> dict:
        """读数据库数据源的库表结构（**只读 information_schema，不取业务数据**）。

        产出表/列/主键/外键/注释。注释是重点——中文库里关系真值活在 COMMENT 里：
        本 POC 实测的库声明外键只有 8 条，而注释里自称关联的有 68 列。
        """
        tenant_id = require_tenant(tenant_id)
        ds = await self._load(tenant_id, ds_id)
        src, cfg = await self._db_catalog_source(ds)
        catalog = await src.read_catalog(cfg)
        return {
            "data_source_id": ds.id,
            "knowledge_base_id": ds.knowledge_base_id,
            "connection_id": ds.connection_id,
            **catalog.to_dict(),
        }

    # ── 由库表结构生成数据源级 schema ──

    async def generate_schema(self, tenant_id: str, ds_id: str, compiler) -> dict:
        """读该数据源的库表结构 → 交给生成器 → 写成**数据源级**的一份 compiled schema。

        三步的顺序不可换，且**失败不落库**：任何一步出错都直接抛出，绝不留下半份 schema。

        `compiler` 由 capability 层传入（`self.service.compiler`）——本服务不持有它，
        免得两个服务互相 import 出环。

        生成默认在本进程内（`jonex_core.capability.atomic.schema_gen`）；只有
        `SCHEMA_GEN_BACKEND=remote` 时才回退到 onto-intelligent 的
        `POST {ONTO_INTEL_BASE}/schema-gen`。
        **只传结构、不传凭证**：密码在上面 `_db_catalog_source()` 里解过一次、
        用来连库读 catalog，到这里已经用完了，不会离开本进程。
        """
        tenant_id = require_tenant(tenant_id)
        ds = await self._load(tenant_id, ds_id)

        backend = schema_gen_backend.resolve_backend()
        # remote 的地址检查放在 read_catalog 之前：保持迁移前「配置缺失直接 1001、
        # 不做无谓连库」的行为。
        base = schema_gen_backend.require_remote_base() if backend == "remote" else ""

        catalog = await self.read_catalog(tenant_id, ds_id)

        # domain_id 是生成器给这份 schema 的域标识（实体名前缀 / 归属），
        # 必须取**真库名**。别读 `ds.config_json`：引用行那份是 `{}` 占位，
        # 会静默退化成 `ds.name`（人类的展示名，不是库名）。上面刚取回的
        # catalog 两条路径都带真 `database`（内联行 = 连库用的那个库名），
        # 拿它当来源，两条路径才对得上。
        domain_id = str(catalog.get("database") or ds.name or ds_id)

        if backend == "remote":
            result = await schema_gen_backend.generate_remote(
                base, catalog, domain_id, ds.name)
        else:
            result = await schema_gen_backend.generate_local(
                catalog, domain_id, ds.name)

        if not result.get("ok"):
            raise InvalidParameterError(message=translate(
                "err.datasource.schema_gen_failed",
                params={"error": str(result.get("error"))[:300]},
                fallback=f"schema 生成失败：{str(result.get('error'))[:300]}",
            ))

        # 乐观锁：已有这份就按它的版本比；没有就传 **0**（表示「我认为这份还不存在」）。
        #
        # ⚠ 不能传 None。`save_compiled_schema` 会**无条件**拒绝 None
        # （ontology_compiler.py:437-439：「编辑器保存必须携带版本，做乐观锁；
        # 缺失直接 400（避免无版本请求绕过并发保护）」）。
        # 而 INSERT 路径本来就不看这个值（repository 只在 old 存在时才比对），
        # 所以 0 是「没有现存版本」的正确表达。
        current = await compiler.get_compiled_schema(tenant_id, ds.knowledge_base_id, data_source_id=ds_id)
        expected = int((current or {}).get("schema_version") or 0)

        entity_types = result.get("entity_types") or []
        # 物理声明外键补进关系图（2026-10-30）：生成器可能漏 FK，② 的 join 图只认
        # `relation_types`；只补 origin=declared、去重/去反向、不猜歧义表名。
        relation_types, fk_added = merge_declared_fks(
            entity_types, result.get("relation_types") or [], catalog)

        saved = await compiler.save_compiled_schema(
            tenant_id=tenant_id,
            knowledge_base_id=ds.knowledge_base_id,
            entity_types=entity_types,
            relation_types=relation_types,
            data_source_id=ds_id,
            expected_schema_version=expected,
        )

        # ── 有界探测（P1-1）：把「这一列里有什么值」变成可审计证据 ──
        # ⚠️ **放在保存之后、且整段兜住异常**：schema 是主产物，证据是增益 ——
        # 探不动（连不上/超时/没有只读凭据）绝不能把生成出来的 schema 也赔进去。
        try:
            tables = _schema_tables_for_probe(result.get("entity_types") or [])
            resolved = await self.query_config(tenant_id, ds_id)
            if resolved is None:
                logger.info("源未登记只读凭据，跳过证据探测: ds=%s", ds_id)
            else:
                source, probe_cfg = resolved
                evidence = await probe_source(source, probe_cfg, tables)
                await compiler.set_evidence(tenant_id, ds.knowledge_base_id, ds_id, evidence)
        except Exception as exc:  # noqa: BLE001 —— 证据探测失败**不该**影响 schema 生成
            logger.warning("证据探测失败（不影响 schema 生成）: ds=%s err=%s", ds_id, exc)

        logger.info(
            "由 catalog 生成 schema: ds=%s kb=%s backend=%s entities=%d relations=%d(+fk=%d) v=%s",
            ds_id, ds.knowledge_base_id, backend, len(entity_types), len(relation_types),
            fk_added, (saved or {}).get("schema_version"),
        )

        # schema 已是 active：把该源的数据资产子图收敛到新版本（best-effort）。
        await project_source_best_effort(tenant_id, ds.knowledge_base_id, ds_id)

        # 收敛子图可能 DETACH DELETE 掉被改名/删除的表节点，挂在其上的 `MAPS_TO`
        # 随节点一起消失 —— 随后按 active 对齐行重挂，否则改名后 active 对齐会
        # 一直「已对齐但无边」，直到有人手工重建。投影同样 best-effort。
        await rebuild_alignments_best_effort(tenant_id, ds.knowledge_base_id)

        stats = dict(result.get("stats") or {})
        stats["fk_relations_added"] = fk_added
        return {
            "data_source_id": ds_id,
            "knowledge_base_id": ds.knowledge_base_id,
            "schema_version": (saved or {}).get("schema_version"),
            "stats": stats,
            "losses": result.get("losses") or [],
            "warnings": result.get("warnings") or [],
        }

    # ── 手工重建数据资产子图（P1.5a）──

    async def rebuild_data_assets(self, tenant_id: str, kb_id: str,
                                  data_source_id: str | None = None) -> dict:
        """把知识库下 database 源的图重建到「当前 schema + 活血缘」形态（幂等）。

        只重建 `access_type == "database"` 的源：ETL 源没有自己的编译 schema，
        它的血缘边归属于被引用的 database 源（`config_json.data_source_id`），
        单独重建 ETL 源只会命中「无 active compiled schema」而空转。

        显式传 `data_source_id` 时**绕过软删过滤**加载该行：源删除后的图清理是
        best-effort 的（见 `delete_source`），Neo4j 故障时 delete 照常提交、子图却
        留在图里；而活源枚举（`list_by_kb` 过滤 `is_deleted`）永远看不见它，孤儿
        scope 只能靠显式 id 走到这里回收（spec §3.2「源删除 → 删除全部
        DataSourceEntity 与边」）。已删 / 非 database 的源 → 清 scope，不重建；
        行不存在、跨租户或跨知识库 → 1002。
        """
        tenant_id = require_tenant(tenant_id)
        if not config.ONTOLOGY_DATA_PROJECTION_ENABLED:
            # 回滚闸门：开关关闭时**零 DB 交互**（这是刻意保留的保证，不能为了
            # 校验 KB 在关档时多开一次会话）。
            return {"disabled": True, "sources": []}
        # KB 校验（1002/404）：否则 typo 的 kb_id 会得到 200 + 空 sources，
        # 与「这个 KB 没有 database 源」同形 —— 调用方分不清「重建成功但无事可做」
        # 与「KB 打错了」。
        from ..repository.knowledge_info_repository import KnowledgeInfoRepository
        async with get_db_session() as session:
            await KnowledgeInfoRepository(session).get_required(kb_id, tenant_id)
        if data_source_id:
            async with get_db_session() as session:
                row = await session.get(KnowledgeDataSource, data_source_id)
            if (row is None or row.tenant_id != tenant_id
                    or row.knowledge_base_id != kb_id):
                raise ResourceNotFoundError(message=translate(
                    "err.datasource.not_found", fallback="数据源不存在"))
            if row.is_deleted or row.access_type != "database":
                # 恢复清理：delete 已成功但图没清掉的源，删 scope 就是修复动作本身。
                # 失败按单源容错口径记账、不 500（清理幂等，调用方可安全重试）。
                try:
                    await OntologyDataProjectionService().delete_source(
                        tenant_id, kb_id, data_source_id)
                    return {"disabled": False, "cleaned": True, "sources": [{
                        "data_source_id": data_source_id, "cleaned": True}]}
                except Exception as exc:  # noqa: BLE001 —— 与逐源重建同口径
                    return {"disabled": False, "cleaned": False, "sources": [{
                        "data_source_id": data_source_id, "error": str(exc)[:300]}]}
            rows = [row]
        else:
            async with get_db_session() as session:
                # ⚠️ `list_by_kb` 默认 limit=100，不显式放宽会让第 101 个源**静默漏重建**。
                rows = await KnowledgeDataSourceRepository(session).list_by_kb(
                    tenant_id, kb_id, limit=1000)
        targets = [r for r in rows
                   if r.access_type == "database"
                   and (not data_source_id or r.id == data_source_id)]
        results = []
        for r in targets:
            try:
                results.append({
                    "data_source_id": r.id,
                    **(await OntologyDataProjectionService().rebuild_source(
                        tenant_id, kb_id, r.id)),
                })
            except Exception as exc:
                # 逐源隔离：一个源失败（如 Neo4j 故障）不中断整次重建，调用方
                # 能从 sources 里看出哪些成功、哪些失败；重建幂等，重试安全。
                results.append({
                    "data_source_id": r.id,
                    "error": str(exc)[:300],
                })
        # 对齐边挂在两侧节点上：源子图先重建/收敛，再重挂 `MAPS_TO`（否则新表
        # 节点还没出现）。**源列表为空也调**：撤销对齐后没有 database 源时，
        # 这一步仍是清理陈旧边的唯一机会（服务内部对「无 active 行且无陈旧边」
        # 有快速跳过）。手工重建端点要回报本次摘要，所以直接调服务而不是
        # best-effort 包装；失败按逐源同一容错口径记成 `alignments.error`。
        try:
            alignments = await OntologyDataProjectionService().rebuild_alignments(
                tenant_id, kb_id)
        except Exception as exc:  # noqa: BLE001 —— 与逐源重建同口径
            alignments = {"error": str(exc)[:300]}
        return {"disabled": False, "sources": results, "alignments": alignments}

    # ── 立即同步（出站 api / storage）──
    async def sync_source(self, tenant_id: str, ds_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        ds = await self._load(tenant_id, ds_id)
        if ds.access_type not in {"api", "storage"}:
            raise InvalidParameterError(message=translate("err.datasource.sync_unsupported", params={"access_type": ds.access_type}, fallback=f"{ds.access_type} 不支持立即同步"))

        # [jonex] 禁用空间拒绝同步：在改状态之前判，避免把 last_sync_status 写成 failed。
        await self._require_active_space_of_kb(tenant_id, ds.knowledge_base_id)

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

    # ── 重置 ingest key（api_push / etl）──
    async def reset_ingest_key(self, tenant_id: str, ds_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeDataSourceRepository(session)
            ds = await repo.get_required(ds_id, tenant_id)
            access_type = ds.access_type
            if access_type not in ("api_push", "etl"):
                # 文案不点名具体类型：api_push 与 etl 都支持重置，写死「仅 API 推送」会误导。
                raise InvalidParameterError(message=translate(
                    "err.datasource.reset_key_unsupported",
                    fallback="该数据源类型不支持重置 Key"))
            plain = generate_ingest_key(tenant_id=tenant_id, kb_id=ds.knowledge_base_id, ds_id=ds_id)
            cfg = dict(ds.config_json or {})
            cfg["ingest_key_hash"] = hash_ingest_key(plain)  # 只换哈希，其余配置保留
            ds.config_json = cfg
            await session.flush()
            result = ds.to_dict()
            await session.commit()
        result["ingest_key"] = plain
        result["ingest_url"] = (
            self._lineage_ingest_url(ds_id) if access_type == "etl" else self._ingest_url(ds_id)
        )
        return result

    # ── ETL 来源：血缘边文件导入（JSON/CSV）──
    async def import_lineage_file(self, tenant_id: str, ds_id: str,
                                  filename: str, content: bytes,
                                  *, user_id: str | None = None) -> dict:
        """ETL 来源的文件导入（JSON/CSV）——只改血缘边，不碰文档。"""
        tenant_id = require_tenant(tenant_id)
        ds = await self._load_owned_etl_source(tenant_id, ds_id)
        cfg = ds.config_json or {}
        if cfg.get("etl_kind") != "file":
            raise InvalidParameterError("该 ETL 来源不是 file 类型")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise InvalidParameterError("文件必须是 UTF-8 文本") from exc
        from .lineage_import import parse_edges
        rows = parse_edges(filename, text)
        if len(rows) > config.LINEAGE_IMPORT_MAX_ROWS:
            raise InvalidParameterError(
                f"单次最多导入 {config.LINEAGE_IMPORT_MAX_ROWS} 行（本次 {len(rows)}）")
        from .lineage_edge_service import LineageEdgeService
        async with get_db_session() as session:
            result = await LineageEdgeService(session).import_edges(
                tenant_id, ds.knowledge_base_id, cfg["data_source_id"], rows,
                user_id=user_id)
        await self._set_sync_status(
            tenant_id, ds_id, "success",
            f"imported={result['imported']} skipped={result['skipped']} "
            # `errors` 列表只回前 20 条；文案要报**全量**错误数（`error_count`），
            # 否则 25 个坏行看起来像只有 20 个。
            f"errors={result['error_count']}",
            touch_at=True)
        # 血缘边是 ETL 源的产物、图挂在被引用的 database 源上。
        await project_source_best_effort(
            tenant_id, ds.knowledge_base_id, cfg["data_source_id"])
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

        # [jonex] 入站推送是公开端点（ingest key 自包含租户），不经过 capability 的
        # 空间判定；禁用空间必须在这里拒绝，否则外部系统仍能持续写入。
        await self._require_active_space_of_kb(tenant_id, kb_id)

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

    # ── ETL 来源：血缘推送接收（OpenLineage / OpenMetadata / DataHub）──
    async def ingest_lineage_event(self, tenant_id: str, ds_id: str,
                                   ingest_key: str, event: dict) -> dict:
        """推送入口：验 key → 按 `etl_kind` 解析 → 幂等 upsert/删除 → 记同步状态。

        三家都是**推送**：OpenLineage 事件、OpenMetadata ChangeEvent、DataHub
        MetadataChangeLog。没有列级血缘的事件（非 COMPLETE / 只有表级）被**忽略**
        （记 success + 空结果），不视为错误；返回形状与 `import_edges` 一致，
        额外带 `deleted`（平台删除事件清掉的 edge 数）。
        """
        tenant_id = require_tenant(tenant_id)
        ds = await self._load_owned_etl_source(tenant_id, ds_id)
        cfg = ds.config_json or {}
        kind = str(cfg.get("etl_kind") or "")
        if kind not in ("openlineage", "openmetadata", "datahub"):
            raise InvalidParameterError(
                f"该 ETL 来源不是推送类型（当前: {kind or '未知'}）")
        if not verify_ingest_key(ingest_key, cfg.get("ingest_key_hash") or ""):
            raise InvalidApiKeyError(message=translate("err.ingest.key_invalid", fallback="ingest key 无效"))

        from .lineage_edge_service import LineageEdgeService
        from .lineage_platform import ParsedLineage, parse_platform_event

        known_tables = None
        if kind in ("openmetadata", "datahub"):
            # 表名归一优先按目标源编译 schema 的已知表（平台 FQN 带 service/catalog）。
            # 取不到 schema 就回退最后两段 —— import_edges 仍会按 schema 如实报错。
            try:
                async with get_db_session() as session:
                    known_tables = await LineageEdgeService(session).schema_tables(
                        tenant_id, ds.knowledge_base_id, cfg["data_source_id"])
            except InvalidParameterError:
                known_tables = None

        if kind == "openlineage":
            from .openlineage import parse_openlineage_event
            parsed = ParsedLineage(
                rows=parse_openlineage_event(event, allowed_namespace=cfg.get("namespace")))
        else:
            parsed = parse_platform_event(kind, event, known_tables=known_tables)

        if len(parsed.rows) > config.LINEAGE_IMPORT_MAX_ROWS:
            raise InvalidParameterError(
                f"单次事件最多导入 {config.LINEAGE_IMPORT_MAX_ROWS} 行（本次 {len(parsed.rows)}）")

        deleted = 0
        if parsed.deletes:
            async with get_db_session() as session:
                edge_service = LineageEdgeService(session)
                for item in parsed.deletes:
                    deleted += await edge_service.delete_platform_edges(
                        tenant_id, ds.knowledge_base_id, cfg["data_source_id"], **item)

        if not parsed.rows:
            # 非 COMPLETE / 没有列级血缘 / 纯删除事件：不是错误——照常记 success。
            message = (f"deleted={deleted}" if parsed.deletes
                       else "no column-level lineage in event")
            await self._set_sync_status(
                tenant_id, ds_id, status="success",
                message=message,
                touch_at=True,
                # 空事件（非 COMPLETE / 无 columnLineage）不许覆盖上一条成功摘要；
                # 删除事件是真实变更，必须把 deleted 写进状态。
                preserve_message_on_success=not parsed.deletes)
            # 空事件/删除事件也收敛一次：上一轮写入的图仍可能落后于当前活行。
            await project_source_best_effort(
                tenant_id, ds.knowledge_base_id, cfg["data_source_id"])
            return {"imported": 0, "skipped": 0, "errors": [],
                    "error_count": 0, "total": 0, "deleted": deleted}

        async with get_db_session() as session:
            result = await LineageEdgeService(session).import_edges(
                tenant_id, ds.knowledge_base_id, cfg["data_source_id"], parsed.rows)
        suffix = f" deleted={deleted}" if deleted else ""
        await self._set_sync_status(
            tenant_id, ds_id, status="success",
            message=f"accepted={result['imported']} skipped={result['skipped']} "
                    # `errors` 列表只回前 20 条；文案要报**全量**错误数（`error_count`），
                    # 否则 25 个坏行看起来像只有 20 个。
                    f"errors={result['error_count']}{suffix}",
            touch_at=True)
        await project_source_best_effort(
            tenant_id, ds.knowledge_base_id, cfg["data_source_id"])
        result["deleted"] = deleted
        return result

    # ── helpers ──
    async def _load(self, tenant_id: str, ds_id: str) -> KnowledgeDataSource:
        async with get_db_session() as session:
            repo = KnowledgeDataSourceRepository(session)
            return await repo.get_required(ds_id, tenant_id)

    async def _load_owned_etl_source(
        self, tenant_id: str, ds_id: str,
    ) -> KnowledgeDataSource:
        """取本租户的 ETL 来源行；不是 ETL ⇒ 1001（租户/软删由仓储 `get_required` 收口）。"""
        async with get_db_session() as session:
            ds = await KnowledgeDataSourceRepository(session).get_required(ds_id, tenant_id)
        if ds.access_type != "etl":
            raise InvalidParameterError(f"数据源 {ds_id} 不是 ETL 来源")
        return ds
    async def _require_active_space_of_kb(self, tenant_id: str, kb_id: str) -> None:
        """数据源链路（出站同步 / 入站推送）**不经过** capability 的空间判定，
        这里补一次资源可用性闸 —— 否则禁用空间仍会被后台同步或外部推送写入数据，
        「所有人都无法使用」只挡住了浏览器。"""
        from .space_permission_service import require_space_active

        async with get_db_session() as session:
            row = (await session.execute(
                text(
                    "SELECT space_id FROM knowledge_base.knowledge_info "
                    "WHERE id = :kb AND tenant_id = :t AND is_deleted = 0"
                ),
                {"kb": kb_id, "t": tenant_id},
            )).first()
        if not row or not row[0]:
            raise ResourceNotFoundError(
                message=translate(
                    "err.kb.not_found",
                    params={"kb_id": kb_id},
                    fallback=f"知识库不存在: {kb_id}",
                )
            )
        await require_space_active(tenant_id, row[0])

    def _ingest_url(self, ds_id: str) -> str:
        base = os.getenv("PUBLIC_API_BASE", "").rstrip("/")
        return f"{base}/api/v1/knowledge-base/ingest/{ds_id}"

    def _lineage_ingest_url(self, ds_id: str) -> str:
        base = os.getenv("PUBLIC_API_BASE", "").rstrip("/")
        return f"{base}/api/v1/knowledge-base/lineage/ingest/{ds_id}"

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

    async def _set_sync_status(self, tenant_id, ds_id, status, message, *,
                               touch_at: bool = False,
                               preserve_message_on_success: bool = False):
        """记同步状态。`touch_at=True` 时**任何**状态都推进 `last_sync_at`。

        默认 false 保持既有调用方语义（`sync_source` 只在开跑时打点）；ETL 的
        文件导入 / OpenLineage 摄入没有 running 阶段，只回 success —— 不显式
        touch，`last_sync_at` 会永远停在 None（真机复现）。

        `preserve_message_on_success=True` 且**原状态就是 success** 时只更新
        状态/时间、保留原 `last_sync_message`：OpenLineage 多事件流里，空的
        COMPLETE 事件不该把上一次「accepted=1…」的导入摘要盖掉。
        """
        async with get_db_session() as session:
            repo = KnowledgeDataSourceRepository(session)
            ds = await repo.get_required(ds_id, tenant_id)
            previous_status = ds.last_sync_status
            ds.last_sync_status = status
            if not (preserve_message_on_success and previous_status == "success"):
                ds.last_sync_message = message
            if status == "running" or touch_at:
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
