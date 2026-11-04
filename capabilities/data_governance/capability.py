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

from .asset_identity import DERIVED_TYPES
from .clients.knowledge_base_client import reset_knowledge_base_client
from .repository.retention_policy_repository import RETENTION_FIELDS
from .services import (
    classification_service,
    clearance_service,
    retention_policy_service,
    standard_catalog_service,
)
from .services.binding_service import (
    SOURCES as BINDING_SOURCES,
    STATUSES as BINDING_STATUSES,
    BindingService,
)
from .services.catalog_service import CatalogService
from .services.contract_service import DataContractService
from .services.lineage_service import build_graph
from .services.lineage_sources import get_lineage_sources
from .services.resource_view_service import ResourceViewService
from .services.source_anchor_service import SourceAnchorService
from .services.standard_asset_service import StandardAssetService
from .services.quality_rule_service import QualityRuleService
from .services.quality_run_service import QualityRunService
from .services.quality_issue_service import QualityIssueService

logger = logging.getLogger(__name__)

#: 标准资产服务是进程级无状态实例（与 `standard_catalog_service` 同模式）。
standard_service = StandardAssetService()

#: 数据合同服务同模式：无状态，按请求传 tenant/user。
contract_service = DataContractService()

#: W2.5 质量闭环三个无状态服务。
quality_rule_service = QualityRuleService()
quality_run_service = QualityRunService()
quality_issue_service = QualityIssueService()

#: W2.6 资源视图三个无状态服务：业务域/责任聚合、落标绑定、原文锚点。
resource_view_service = ResourceViewService()
binding_service = BindingService()
source_anchor_service = SourceAnchorService()

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
    "list_standard_assets": (),
    "get_standard_asset": ("asset_id",),
    "list_standard_asset_versions": ("kind", "code"),
    "create_standard_draft": ("kind", "code", "name", "payload"),
    "update_standard_draft": ("asset_id", "expected_revision"),
    "publish_standard_asset": ("asset_id", "expected_revision"),
    "retire_standard_asset": ("asset_id",),
    "delete_standard_draft": ("asset_id",),
    "list_data_contracts": (),
    "get_data_contract": ("contract_id",),
    "list_data_contract_versions": ("data_source_id",),
    "preview_data_contract_scaffold": ("data_source_id",),
    "create_data_contract_draft": ("data_source_id",),
    "update_data_contract_draft": ("contract_id", "expected_revision"),
    "publish_data_contract": ("contract_id", "expected_revision"),
    "retire_data_contract": ("contract_id",),
    "delete_data_contract_draft": ("contract_id",),
    "import_data_contract": ("data_source_id", "content"),
    "export_data_contract": ("contract_id",),
    # ── W2.5 数据质量闭环 ──
    "list_quality_rules": (),
    "get_quality_rule": ("rule_id",),
    "create_quality_rule": (
        "name", "asset_type", "asset_id", "dimension", "severity",
        "check_type", "definition", "threshold",
    ),
    "update_quality_rule": ("rule_id", "expected_revision"),
    "delete_quality_rule": ("rule_id",),
    "import_quality_rules_from_contract": ("contract_id",),
    "preview_quality_check_sql": ("data_source_id",),
    "create_quality_run": ("data_source_id",),
    "submit_quality_run_results": ("run_id", "results"),
    "fail_quality_run": ("run_id", "message"),
    "list_quality_runs": (),
    "get_quality_run": ("run_id",),
    "list_quality_issues": (),
    "get_quality_issue": ("issue_id",),
    "assign_quality_issue": ("issue_id", "assignee_id", "expected_revision"),
    "update_quality_issue_status": ("issue_id", "to_status", "expected_revision"),
    "verify_quality_issue": ("issue_id", "passed", "expected_revision"),
    "comment_quality_issue": ("issue_id", "note"),
    "get_quality_summary": (),
    # ── W2.6 资源视图 / 落标绑定 / 原文锚点 ──
    "get_resource_domain_view": (),
    "get_responsibility_view": ("group_by",),
    "list_standard_bindings": (),
    "create_standard_binding": (
        "standard_kind", "standard_code", "asset_type", "asset_id",
    ),
    "update_standard_binding": ("binding_id", "expected_revision"),
    "delete_standard_binding": ("binding_id",),
    "suggest_standard_bindings": (),
    "get_standard_binding_view": (),
    "get_asset_source_anchors": ("asset_type", "asset_id"),
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

#: 标准资产相关 action：`validate_input` 对可选字段做显式类型校验，
#: 避免 list/dict 等不可哈希值穿透到 `in` / 下游 service。
_STANDARD_ASSET_ACTIONS = frozenset({
    "list_standard_assets",
    "get_standard_asset",
    "list_standard_asset_versions",
    "create_standard_draft",
    "update_standard_draft",
    "publish_standard_asset",
    "retire_standard_asset",
    "delete_standard_draft",
})

#: 数据合同相关 action：`validate_input` 对可选字段做显式类型校验
#: （`expected_revision` 的 bool 是 int 子类，必须显式排除）。
_DATA_CONTRACT_ACTIONS = frozenset({
    "list_data_contracts",
    "get_data_contract",
    "list_data_contract_versions",
    "preview_data_contract_scaffold",
    "create_data_contract_draft",
    "update_data_contract_draft",
    "publish_data_contract",
    "retire_data_contract",
    "delete_data_contract_draft",
    "import_data_contract",
    "export_data_contract",
})

#: W2.5 质量闭环 actions：`validate_input` 对可选字段做显式类型校验
#: （bool 是 int 子类，`enabled` / `expected_revision` 必须显式排除）。
_QUALITY_ACTIONS = frozenset({
    "list_quality_rules", "get_quality_rule", "create_quality_rule",
    "update_quality_rule", "delete_quality_rule",
    "import_quality_rules_from_contract", "preview_quality_check_sql",
    "create_quality_run", "submit_quality_run_results", "fail_quality_run",
    "list_quality_runs", "get_quality_run", "list_quality_issues",
    "get_quality_issue", "assign_quality_issue",
    "update_quality_issue_status", "verify_quality_issue",
    "comment_quality_issue", "get_quality_summary",
})

#: W2.6 资源视图 actions：`validate_input` 统一做 id/枚举/整数类型校验
#: （网关把未传的可选字段序列化成 None，None 等价于「未提供」）。
_RESOURCE_VIEW_ACTIONS = frozenset({
    "get_resource_domain_view",
    "get_responsibility_view",
    "list_standard_bindings",
    "create_standard_binding",
    "update_standard_binding",
    "delete_standard_binding",
    "suggest_standard_bindings",
    "get_standard_binding_view",
    "get_asset_source_anchors",
})

#: 责任视图分组维度（spec §4）。
_GROUP_BY_VALUES = frozenset({"owner", "domain"})

#: 建议匹配条数上限（与 service `_require_limit` 同口径）。
_MAX_SUGGEST_LIMIT = 100

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
            direction = data.get("direction", "both")
            depth = data.get("depth", 1)
            if asset.get("asset_type") in DERIVED_TYPES:
                from .services.asset_lineage import build_data_asset_graph

                return await build_data_asset_graph(
                    request.tenant_id, asset,
                    direction=direction, depth=depth,
                )
            graph = await build_graph(                asset=asset,
                # 只在键缺失时默认；空串交给 build_graph 校验并返回 400。
                direction=direction,
                depth=depth,
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
            "list_standard_assets": lambda r, d: standard_service.list_assets(
                r.tenant_id, r.user_id, d),
            "get_standard_asset": lambda r, d: standard_service.get_asset(
                r.tenant_id, r.user_id, d["asset_id"]),
            "list_standard_asset_versions": lambda r, d: standard_service.list_versions(
                r.tenant_id, r.user_id, d["kind"], d["code"]),
            "create_standard_draft": lambda r, d: standard_service.create_draft(
                r.tenant_id, r.user_id, d["kind"], d["code"], d["name"], d["payload"],
                base_version=d.get("base_version")),
            "update_standard_draft": lambda r, d: standard_service.update_draft(
                r.tenant_id, r.user_id, d["asset_id"], d["expected_revision"],
                name=d.get("name"), payload=d.get("payload")),
            "publish_standard_asset": lambda r, d: standard_service.publish(
                r.tenant_id, r.user_id, d["asset_id"], d["expected_revision"]),
            "retire_standard_asset": lambda r, d: standard_service.retire(
                r.tenant_id, r.user_id, d["asset_id"]),
            "delete_standard_draft": lambda r, d: standard_service.delete_draft(
                r.tenant_id, r.user_id, d["asset_id"]),
            "list_data_contracts": lambda r, d: contract_service.list(
                r.tenant_id, r.user_id, d),
            "get_data_contract": lambda r, d: contract_service.get(
                r.tenant_id, r.user_id, d["contract_id"]),
            "list_data_contract_versions": lambda r, d: contract_service.versions(
                r.tenant_id, r.user_id, d["data_source_id"]),
            "preview_data_contract_scaffold": (
                lambda r, d: contract_service.preview_scaffold(
                    r.tenant_id, r.user_id, d["data_source_id"])
            ),
            "create_data_contract_draft": (
                lambda r, d: contract_service.create_draft(
                    r.tenant_id, r.user_id, d["data_source_id"],
                    payload=d.get("payload"), name=d.get("name"),
                    base_version=d.get("base_version"),
                )
            ),
            "update_data_contract_draft": (
                lambda r, d: contract_service.update_draft(
                    r.tenant_id, r.user_id, d["contract_id"],
                    d["expected_revision"],
                    name=d.get("name"), payload=d.get("payload"),
                )
            ),
            "publish_data_contract": lambda r, d: contract_service.publish(
                r.tenant_id, r.user_id, d["contract_id"],
                d["expected_revision"]),
            "retire_data_contract": lambda r, d: contract_service.retire(
                r.tenant_id, r.user_id, d["contract_id"]),
            "delete_data_contract_draft": (
                lambda r, d: contract_service.delete_draft(
                    r.tenant_id, r.user_id, d["contract_id"])
            ),
            "import_data_contract": lambda r, d: contract_service.import_contract(
                r.tenant_id, r.user_id, d["data_source_id"], d["content"],
                format=d.get("format")),
            "export_data_contract": lambda r, d: contract_service.export_contract(
                r.tenant_id, r.user_id, d["contract_id"],
                format=d.get("format")),
            # ── W2.5 数据质量闭环 ──
            "list_quality_rules": lambda r, d: quality_rule_service.list_rules(
                r.tenant_id, r.user_id, d),
            "get_quality_rule": lambda r, d: quality_rule_service.get_rule(
                r.tenant_id, r.user_id, d["rule_id"]),
            "create_quality_rule": lambda r, d: quality_rule_service.create_rule(
                r.tenant_id, r.user_id, d),
            "update_quality_rule": lambda r, d: quality_rule_service.update_rule(
                r.tenant_id, r.user_id, d["rule_id"],
                d["expected_revision"], d),
            "delete_quality_rule": lambda r, d: quality_rule_service.delete_rule(
                r.tenant_id, r.user_id, d["rule_id"]),
            "import_quality_rules_from_contract": (
                lambda r, d: quality_rule_service.import_from_contract(
                    r.tenant_id, r.user_id, d["contract_id"])
            ),
            "preview_quality_check_sql": lambda r, d: quality_run_service.preview_sql(
                r.tenant_id, r.user_id, d),
            "create_quality_run": lambda r, d: quality_run_service.create_run(
                r.tenant_id, r.user_id, d),
            "submit_quality_run_results": (
                lambda r, d: quality_run_service.submit_results(
                    r.tenant_id, r.user_id, d["run_id"], d["results"],
                    executor=d.get("executor"), message=d.get("message"),
                )
            ),
            "fail_quality_run": lambda r, d: quality_run_service.fail_run(
                r.tenant_id, r.user_id, d["run_id"], d["message"]),
            "list_quality_runs": lambda r, d: quality_run_service.list_runs(
                r.tenant_id, r.user_id, d),
            "get_quality_run": lambda r, d: quality_run_service.get_run(
                r.tenant_id, r.user_id, d["run_id"]),
            "list_quality_issues": lambda r, d: quality_issue_service.list_issues(
                r.tenant_id, r.user_id, d),
            "get_quality_issue": lambda r, d: quality_issue_service.get_issue(
                r.tenant_id, r.user_id, d["issue_id"]),
            "assign_quality_issue": lambda r, d: quality_issue_service.assign(
                r.tenant_id, r.user_id, d["issue_id"], d["assignee_id"],
                d["expected_revision"]),
            "update_quality_issue_status": (
                lambda r, d: quality_issue_service.update_status(
                    r.tenant_id, r.user_id, d["issue_id"], d["to_status"],
                    d["expected_revision"], note=d.get("note"),
                )
            ),
            "verify_quality_issue": lambda r, d: quality_issue_service.verify(
                r.tenant_id, r.user_id, d["issue_id"], d["passed"],
                d.get("note"), d["expected_revision"]),
            "comment_quality_issue": lambda r, d: quality_issue_service.comment(
                r.tenant_id, r.user_id, d["issue_id"], d["note"]),
            "get_quality_summary": lambda r, d: quality_issue_service.summary(
                r.tenant_id, r.user_id, data_source_id=d.get("data_source_id")),
            # ── W2.6 资源视图 / 落标绑定 / 原文锚点 ──
            "get_resource_domain_view": (
                lambda r, d: resource_view_service.domain_view(
                    r.tenant_id, r.user_id, d)
            ),
            "get_responsibility_view": (
                lambda r, d: resource_view_service.responsibility_view(
                    r.tenant_id, r.user_id, d)
            ),
            "list_standard_bindings": lambda r, d: binding_service.list_bindings(
                r.tenant_id, r.user_id, d),
            "create_standard_binding": (
                lambda r, d: binding_service.create_binding(
                    r.tenant_id, r.user_id, d)
            ),
            "update_standard_binding": (
                lambda r, d: binding_service.update_binding(
                    r.tenant_id, r.user_id, d["binding_id"],
                    d["expected_revision"], d)
            ),
            "delete_standard_binding": (
                lambda r, d: binding_service.delete_binding(
                    r.tenant_id, r.user_id, d["binding_id"])
            ),
            "suggest_standard_bindings": (
                lambda r, d: binding_service.suggest_bindings(
                    r.tenant_id, r.user_id, d)
            ),
            # 覆盖率视图：service 方法名是 `coverage`（spec §5.2）。
            "get_standard_binding_view": lambda r, d: binding_service.coverage(
                r.tenant_id, r.user_id, d),
            "get_asset_source_anchors": (
                lambda r, d: source_anchor_service.get_anchors(
                    r.tenant_id, r.user_id, d["asset_type"], d["asset_id"])
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
        if action in _STANDARD_ASSET_ACTIONS:
            # 网关会把未传的可选字段序列化成 None（如 create 的 base_version、
            # list 的 kind/status），None 等价于「未提供」，不能按类型错误拒绝。
            for field in ("kind", "status"):
                value = data.get(field)
                if value is not None and (
                    not isinstance(value, str) or not value.strip()
                ):
                    return False
            for field in ("expected_revision", "base_version"):
                value = data.get(field)
                if value is not None:
                    # bool 是 int 的子类，必须显式排除。
                    if isinstance(value, bool) or not isinstance(value, int):
                        return False
            value = data.get("include_drafts")
            if value is not None and not isinstance(value, bool):
                return False
        if action in _DATA_CONTRACT_ACTIONS:
            # 网关会把未传的可选字段序列化成 None（如 create 的 name/base_version、
            # list 的 status/keyword），None 等价于「未提供」，不能按类型错误拒绝。
            for field in (
                "contract_id", "data_source_id", "name", "format",
                "status", "keyword",
            ):
                value = data.get(field)
                if value is not None and not isinstance(value, str):
                    return False
            for field in ("expected_revision", "base_version"):
                value = data.get(field)
                if value is not None:
                    # bool 是 int 的子类，必须显式排除。
                    if isinstance(value, bool) or not isinstance(value, int):
                        return False
            payload = data.get("payload")
            if payload is not None and not isinstance(payload, dict):
                return False
            content = data.get("content")
            if content is not None and not isinstance(content, str):
                return False
            include_drafts = data.get("include_drafts")
            if include_drafts is not None and not isinstance(include_drafts, bool):
                return False
        if action in _QUALITY_ACTIONS:
            # 网关会把未传的可选字段序列化成 None；None 等价于「未提供」。
            for field in (
                "rule_id", "run_id", "issue_id", "assignee_id", "to_status",
                "note", "message", "keyword", "data_source_id", "asset_type",
                "asset_id", "dimension", "severity", "check_type",
                "contract_id", "trigger", "executor", "dialect", "name",
                "kb_id",
            ):
                value = data.get(field)
                if value is not None and not isinstance(value, str):
                    return False
            value = data.get("expected_revision")
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                return False
            value = data.get("enabled")
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value not in (0, 1)
            ):
                return False
            for field in ("definition", "threshold"):
                value = data.get(field)
                if value is not None and not isinstance(value, dict):
                    return False
            results = data.get("results")
            if results is not None and not isinstance(results, list):
                return False
            rule_ids = data.get("rule_ids")
            if rule_ids is not None and (
                not isinstance(rule_ids, list)
                or not all(isinstance(item, str) for item in rule_ids)
            ):
                return False
            passed = data.get("passed")
            if passed is not None and not isinstance(passed, bool):
                return False
        if action in _RESOURCE_VIEW_ACTIONS:
            # 网关会把未传的可选字段序列化成 None；None 等价于「未提供」。
            # 先做字符串类型再做集合成员判断：list/dict 不可哈希。
            for field in (
                "space_id", "domain", "group_by", "standard_kind",
                "standard_code", "asset_type", "asset_id", "status",
                "source", "note", "binding_id", "kind",
            ):
                value = data.get(field)
                if value is not None and not isinstance(value, str):
                    return False
            group_by = data.get("group_by")
            if group_by is not None and group_by not in _GROUP_BY_VALUES:
                return False
            status = data.get("status")
            if status is not None and status not in BINDING_STATUSES:
                return False
            source = data.get("source")
            if source is not None and source not in BINDING_SOURCES:
                return False
            expected_revision = data.get("expected_revision")
            if expected_revision is not None and (
                isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int)
            ):
                return False
            limit = data.get("limit")
            if limit is not None and (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit < 1
                or limit > _MAX_SUGGEST_LIMIT
            ):
                return False
            for field in ("page", "page_size"):
                value = data.get(field)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int)
                ):
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
