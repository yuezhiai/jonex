"""数据治理业务能力（business.data_governance.v1）。

- 资产目录读写：`CatalogService`（任务 1–4）
- 标准目录只读：`standard_catalog_service`（第 5 期任务 1–3）
- 文档级双向血缘：`build_graph` + `LineageSources`（任务 6）

本能力**不开并行 REST 层**：对外只有 Gateway → Sidecar `/invoke` 一条链路，
网关适配层在 `api_gateway/routes/data_governance.py`。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from jonex_core.capability import BaseCapability
from jonex_core.capability.models import (
    CapabilityMetadata,
    CapabilityRequest,
    CapabilityResponse,
    CapabilityType,
)
from jonex_core.common.exceptions import (
    InvalidParameterError,
    JonexException,
    ResourceNotFoundError,
)
from jonex_core.common.security_guards import assert_secure_jwt_secret
from jonex_core.common.tenant import require_tenant

from .clients.knowledge_base_client import reset_knowledge_base_client
from .repository.retention_policy_repository import RETENTION_FIELDS
from .services import (
    classification_service,
    clearance_service,
    retention_policy_service,
    standard_catalog_service,
)
from .services.catalog_service import CatalogService
from .services.lineage_service import build_graph
from .services.lineage_sources import get_lineage_sources

logger = logging.getLogger(__name__)

ActionHandler = Callable[[CapabilityRequest, dict[str, Any]], Awaitable[Any]]

#: 每个 action 的必填字段；与 `_build_dispatch()` 的 key 集合完全一致
#: （由 `test_data_governance_capability` 校验）。
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "list_assets": (),
    "get_asset": ("asset_type", "asset_id"),
    "update_asset_metadata": ("asset_type", "asset_id"),
    "get_asset_lineage": ("asset_type", "asset_id"),
    "get_retention_policy": ("scope_type",),
    "set_retention_policy": ("scope_type",),
    "list_classification_config": (),
    "upsert_classification_level": ("code",),
    "upsert_classification_category": ("code",),
    "get_classification_default": ("scope_type",),
    "set_classification_default": ("scope_type",),
    "batch_update_classification": ("assets",),
    "list_user_clearances": (),
    # `max_level_code` 故意不在此列：显式 null 表示清除，`_is_empty` 会误判 400。
    "set_user_clearance": ("user_id",),
    "list_standard_domains": (),
    "get_standard_domain": ("domain_id",),
    "get_standard_scenario": ("scenario_id",),
    "list_kb_schemas": (),
    "get_standard_references": ("scope_type", "scope_id"),
    "list_standard_scenario_versions": ("scenario_id",),
    "get_standard_scenario_version": ("scenario_id", "version"),
    "diff_standard_scenario_versions": (
        "scenario_id", "from_version", "to_version",
    ),
    "get_standard_compliance": ("scenario_id",),
}

#: 保留策略作用域；`kb/document` 还要求 `scope_id` 非空。
_RETENTION_SCOPES = frozenset({"tenant", "kb", "document"})
_RETENTION_ACTIONS = frozenset({"get_retention_policy", "set_retention_policy"})
_RETENTION_WRITABLE_FIELDS = RETENTION_FIELDS

#: 标准目录影响分析的对象作用域；与 service 共用同一份真值。
_STANDARD_SCOPES = standard_catalog_service.SCOPES

#: 元数据写入白名单：请求里的其余键（asset_type/asset_id 等）不进 service。
_METADATA_WRITABLE_FIELDS = ("owner", "domain", "sensitivity", "categories")


def _is_empty(value) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


class DataGovernanceCapability(BaseCapability):
    """数据治理能力：资产目录 + 元数据 + 文档血缘。"""

    def __init__(self):
        self._service = CatalogService()
        self._dispatch = self._build_dispatch()
        super().__init__()

    async def initialize(self) -> None:
        # 与 business_domain / knowledge_base 同一道安全守卫。
        assert_secure_jwt_secret()
        await super().initialize()

    async def shutdown(self) -> None:
        # KB 客户端是进程级单例，服务退出时释放连接池。
        await reset_knowledge_base_client()
        await super().shutdown()

    def _build_dispatch(self) -> dict[str, ActionHandler]:
        service = self._service

        async def update_asset_metadata(request, data):
            # 只把白名单四键交给 service；`asset_type/asset_id` 单独传。
            fields = {
                key: data[key]
                for key in _METADATA_WRITABLE_FIELDS
                if key in data
            }
            return await service.update_metadata(
                request.tenant_id,
                request.user_id,
                data.get("asset_type"),
                data.get("asset_id"),
                fields,
                username=request.username,
                ip=request.ip,
            )

        async def get_retention_policy(request, data):
            return await retention_policy_service.get_policy(
                request.tenant_id,
                scope_type=data.get("scope_type"),
                scope_id=data.get("scope_id"),
                user_id=request.user_id,
            )

        async def set_retention_policy(request, data):
            # 只把两个策略字段交给 service（scope 单独传），未知键不进下游。
            fields = {
                key: data[key]
                for key in _RETENTION_WRITABLE_FIELDS
                if key in data
            }
            return await retention_policy_service.set_policy(
                request.tenant_id,
                scope_type=data.get("scope_type"),
                scope_id=data.get("scope_id"),
                fields=fields,
                user_id=request.user_id,
                username=request.username,
                ip=request.ip,
            )

        async def get_asset_lineage(request, data):
            # 写路径同款「读资产」：质量投影对血缘无意义，显式关掉。
            # 根资产超密级时 get_asset 已按 max_rank 过滤并以 404 返回。
            asset = await service.get_asset(
                request.tenant_id,
                request.user_id,
                data.get("asset_type"),
                data.get("asset_id"),
                include_quality=False,
            )
            graph = await build_graph(
                asset=asset,
                # 只在键缺失时默认；空串交给 build_graph 校验并返回 400。
                direction=data.get("direction", "both"),
                depth=data.get("depth", 1),
                sources=get_lineage_sources(request.tenant_id),
            )

            # 上游数据源节点逐个按当前用户密级判定；实体/chunk 节点属于
            # 已放行的根文档，不再单独判定。节点被剔除时同步删掉悬空边。
            nodes = graph.get("nodes")
            if isinstance(nodes, list):
                kept = []
                for node in nodes:
                    if node.get("type") != "data_source":
                        kept.append(node)
                        continue
                    # 节点 id 形如 `data_source:<asset_id>`。
                    raw_id = str(node.get("id") or "")
                    source_id = raw_id.partition(":")[2]
                    try:
                        await service.get_asset(
                            request.tenant_id,
                            request.user_id,
                            "data_source",
                            source_id,
                            include_quality=False,
                        )
                    except ResourceNotFoundError:
                        continue
                    kept.append(node)
                graph["nodes"] = kept

                kept_ids = {node.get("id") for node in kept}
                edges = graph.get("edges")
                if isinstance(edges, list):
                    graph["edges"] = [
                        edge for edge in edges
                        if edge.get("source") in kept_ids
                        and edge.get("target") in kept_ids
                    ]
            return graph

        async def set_user_clearance(request, data):
            # 键必须存在：显式 null 是「清除」，不能当作缺失字段拒绝。
            if "max_level_code" not in data:
                raise InvalidParameterError(message="max_level_code 字段必须存在")
            # 直接 execute（未经 validate_input）时不能 KeyError → 500。
            if not str(data.get("user_id") or "").strip():
                raise InvalidParameterError(message="user_id 字段必须存在")
            return await clearance_service.set_clearance(
                request.tenant_id, request.user_id,
                target_user_id=data.get("user_id"),
                max_level_code=data.get("max_level_code"),
                username=request.username, ip=request.ip,
            )

        return {
            "list_assets": lambda r, d: service.list_assets(
                r.tenant_id, r.user_id, d,
            ),
            "get_asset": lambda r, d: service.get_asset(
                r.tenant_id,
                r.user_id,
                d.get("asset_type"),
                d.get("asset_id"),
                include_quality=True,
            ),
            "update_asset_metadata": update_asset_metadata,
            "batch_update_classification": (
                lambda r, d: service.batch_update_classification(
                    r.tenant_id, r.user_id, d,
                    username=r.username, ip=r.ip,
                )
            ),
            "get_asset_lineage": get_asset_lineage,
            "get_retention_policy": get_retention_policy,
            "set_retention_policy": set_retention_policy,
            "list_classification_config": lambda r, d: classification_service.list_config(
                r.tenant_id, r.user_id,
            ),
            "upsert_classification_level": lambda r, d: classification_service.upsert_level(
                r.tenant_id, r.user_id, code=d.get("code"),
                fields={k: d[k] for k in ("name", "rank", "is_active") if k in d},
                username=r.username, ip=r.ip,
            ),
            "upsert_classification_category": lambda r, d: classification_service.upsert_category(
                r.tenant_id, r.user_id, code=d.get("code"),
                fields={k: d[k] for k in ("name", "is_active") if k in d},
                username=r.username, ip=r.ip,
            ),
            "get_classification_default": lambda r, d: classification_service.get_default(
                r.tenant_id, r.user_id,
                scope_type=d.get("scope_type"), scope_id=d.get("scope_id"),
            ),
            "set_classification_default": lambda r, d: classification_service.set_default(
                r.tenant_id, r.user_id,
                scope_type=d.get("scope_type"), scope_id=d.get("scope_id"),
                fields={k: d[k] for k in ("level_code", "categories") if k in d},
                username=r.username, ip=r.ip,
            ),
            "list_user_clearances": lambda r, d: clearance_service.list_clearances(
                r.tenant_id, r.user_id,
            ),
            "set_user_clearance": set_user_clearance,
            "list_standard_domains": lambda r, d: standard_catalog_service.list_domains(
                r.tenant_id, r.user_id,
            ),
            "get_standard_domain": lambda r, d: standard_catalog_service.get_domain(
                r.tenant_id, r.user_id, d.get("domain_id"),
            ),
            "get_standard_scenario": lambda r, d: standard_catalog_service.get_scenario(
                r.tenant_id, r.user_id, d.get("scenario_id"),
            ),
            "list_kb_schemas": lambda r, d: standard_catalog_service.list_kb_schemas(
                r.tenant_id, r.user_id, d,
            ),
            "get_standard_references": lambda r, d: standard_catalog_service.get_references(
                r.tenant_id, r.user_id, d,
            ),
            "list_standard_scenario_versions": lambda r, d: (
                standard_catalog_service.list_versions(
                    r.tenant_id, r.user_id, d.get("scenario_id"),
                )
            ),
            "get_standard_scenario_version": lambda r, d: (
                standard_catalog_service.get_version(
                    r.tenant_id, r.user_id, d.get("scenario_id"), d.get("version"),
                )
            ),
            "diff_standard_scenario_versions": lambda r, d: (
                standard_catalog_service.diff_versions(
                    r.tenant_id, r.user_id, d.get("scenario_id"),
                    d.get("from_version"), d.get("to_version"),
                )
            ),
            "get_standard_compliance": lambda r, d: (
                standard_catalog_service.get_compliance(
                    r.tenant_id, r.user_id, d.get("scenario_id"),
                )
            ),
        }

    def _build_metadata(self) -> CapabilityMetadata:
        return CapabilityMetadata(
            capability_id="data_governance",
            capability_name="数据治理",
            capability_type=CapabilityType.BUSINESS,
            version="v1",
            description="数据资产目录、标准目录、元数据治理、保留策略与文档级双向血缘",
            author="jonex",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(self._dispatch.keys()),
                        "description": "操作类型",
                    },
                    "data": {"type": "object", "description": "操作数据"},
                },
                "required": ["action"],
            },
            output_schema={"type": "object"},
            tags=["数据治理", "资产目录", "标准目录", "保留策略", "血缘", "分级分类"],
        )

    async def validate_input(self, request: CapabilityRequest) -> bool:
        action = request.payload.get("action")
        if action not in self._dispatch:
            return False
        data = request.payload.get("data") or {}
        if not isinstance(data, dict):
            return False
        if not all(
            not _is_empty(data.get(field))
            for field in _REQUIRED_FIELDS.get(action, ())
        ):
            return False
        if action in _RETENTION_ACTIONS:
            # 先类型后成员：list/dict 不可哈希，直接 `in frozen set` 会抛 TypeError。
            scope_type = data.get("scope_type")
            if (not isinstance(scope_type, str)
                    or scope_type not in _RETENTION_SCOPES):
                return False
            if scope_type != "tenant" and _is_empty(data.get("scope_id")):
                return False
        if action == "get_standard_references":
            # 先类型后成员：list/dict 不可哈希，直接 `in frozen set` 会抛 TypeError。
            scope_type = data.get("scope_type")
            if (not isinstance(scope_type, str)
                    or scope_type not in _STANDARD_SCOPES):
                return False
        if action == "batch_update_classification":
            # `_is_empty` 对 list 恒 False；这里显式要求非空数组，元素类型
            # 交给 service 校验。
            assets = data.get("assets")
            if not isinstance(assets, list) or not assets:
                return False
        if action in {"get_classification_default", "set_classification_default"}:
            # 先类型后成员：list/dict 不可哈希，直接 `in set` 会抛 TypeError。
            scope_type = data.get("scope_type")
            if not isinstance(scope_type, str) or scope_type not in {"tenant", "kb"}:
                return False
            if scope_type == "kb" and _is_empty(data.get("scope_id")):
                return False
        return True

    async def execute(self, request: CapabilityRequest) -> CapabilityResponse:
        action = request.payload.get("action")
        data = request.payload.get("data") or {}
        if not isinstance(data, dict):
            data = {}

        handler = self._dispatch.get(action)
        if handler is None:
            return CapabilityResponse.error(
                request.request_id, 400, f"不支持的操作: {action}",
            )

        try:
            request.tenant_id = require_tenant(request.tenant_id)
            result = await handler(request, data)
            return CapabilityResponse.ok(request.request_id, result)
        except JonexException as e:
            return CapabilityResponse.error(
                request.request_id, e.code, e.message,
            )
        except Exception:
            logger.exception("执行数据治理能力失败: action=%s", action)
            return CapabilityResponse.error(
                request.request_id, 500, "执行数据治理能力失败",
            )
