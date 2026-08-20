"""
业务领域 + 生态管理能力包装类 (business.business_domain.v1)
"""
import logging

from jonex_core.capability import BaseCapability
from jonex_core.capability.models import (
    CapabilityMetadata,
    CapabilityRequest,
    CapabilityResponse,
    CapabilityType,
)
from jonex_core.common.exceptions import TenantIsolationError, JonexException, PermissionDeniedError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from capabilities.business_domain.services import (
    EngineService,
    AdapterService, SkillService, TemplateService,
)
from capabilities.business_domain.services.prompt_template_service import PromptTemplateService

logger = logging.getLogger(__name__)


_ACTION_PERMISSIONS = {
    "create_access_method": "engine:write",
    "update_access_method": "engine:write",
    "create_parser": "engine:write",
    "update_parser": "engine:write",
    "create_provider": "engine:write",
    "update_provider": "engine:write",
    "test_provider": "engine:write",
    "create_adapter": "adapter:write",
    "update_adapter": "adapter:write",
    "connect_adapter": "adapter:write",
    "disconnect_adapter": "adapter:write",
    "enable_skill": "skill:write",
    "disable_skill": "skill:write",
    "create_template_domain": "template:write",
    "update_template_domain": "template:write",
    "delete_template_domain": "template:write",
    "create_template_scenario": "template:write",
    "update_template_scenario": "template:write",
    "delete_template_scenario": "template:write",
    "create_template_object": "template:write",
    "update_template_object": "template:write",
    "delete_template_object": "template:write",
    "create_template_relation": "template:write",
    "update_template_relation": "template:write",
    "delete_template_relation": "template:write",
    "create_prompt_template": "prompt:write",
    "update_prompt_template": "prompt:write",
    "delete_prompt_template": "prompt:write",
    "copy_prompt_template": "prompt:write",
    "rollback_prompt_template": "prompt:write",
    "create_template_constraint": "template:write",
    "update_template_constraint": "template:write",
    "delete_template_constraint": "template:write",
    "import_template_ontology_yaml": "template:write",
    "export_template_ontology_yaml": "template:write",
}


async def _check_action_permission(request) -> None:
    """invoke dispatch 前置校验：action 映射权限码 → 用户权限集合判定。

    user_id 口径（安全决策，已定案）：
    - CapabilityRequest.user_id 是 Optional[str]（gateway 仅在 JWT 可解时透传 sub；
      测试 token 解不出 JWT → user_id 为 None，不是 "0"）。
    - 无 user_id → 放行（不拦截）：能到达 capability 的请求已过 Sidecar 认证，
      权限码校验是租户内细化授权而非边界防线；测试 token 依赖此口径。
    """
    action = request.payload.get("action")
    code = _ACTION_PERMISSIONS.get(action)
    if not code:
        return
    if not request.user_id:
        return
    from jonex_core.security.permission import get_user_permissions

    tenant_id = request.tenant_id
    perms = await get_user_permissions(tenant_id, int(request.user_id))
    if code not in perms:
        raise PermissionDeniedError(
            message=translate("err.auth.insufficient_permission", params={"required": code}, fallback=f"权限不足：需要权限 {code}")
        )


class BusinessDomainCapability(BaseCapability):
    """业务领域 + 生态管理能力"""

    def __init__(self):
        self._engine = EngineService()
        self._adapter = AdapterService()
        self._skill = SkillService()
        self._template = TemplateService()
        self._prompt_template = PromptTemplateService()
        self._dispatch = self._build_dispatch()
        super().__init__()

    def register_routes(self, app):
        """注册业务领域 REST API 路由"""
        from capabilities.business_domain.api import create_router

        router = create_router()
        app.include_router(router, prefix="/api/v1")

    def _build_dispatch(self) -> dict:
        e = self._engine
        a = self._adapter
        sk = self._skill
        t = self._template
        pt = self._prompt_template
        return {
            # ── 引擎管理 ──
            "list_access_methods":    lambda r, d: e.list_access_methods(d.get("offset", 0), d.get("limit", 20)),
            "create_access_method":   lambda r, d: e.create_access_method(d),
            "update_access_method":   lambda r, d: e.update_access_method(d["method_id"], d),
            "list_parsers":           lambda r, d: e.list_parsers(d.get("offset", 0), d.get("limit", 20)),
            "create_parser":          lambda r, d: e.create_parser(d),
            "update_parser":          lambda r, d: e.update_parser(d["parser_id"], d),
            "list_providers":         lambda r, d: e.list_providers(d.get("offset", 0), d.get("limit", 20)),
            "create_provider":        lambda r, d: e.create_provider(d),
            "update_provider":        lambda r, d: e.update_provider(d["provider_id"], d),
            "test_provider":          lambda r, d: e.test_provider(d["provider_id"]),
            # ── 生态适配器 ──
            "list_adapters":          lambda r, d: a.list(r.tenant_id, d.get("offset", 0), d.get("limit", 20)),
            "create_adapter":         lambda r, d: a.create(r.tenant_id, d),
            "update_adapter":         lambda r, d: a.update(d["adapter_id"], r.tenant_id, d),
            "connect_adapter":        lambda r, d: a.connect(d["adapter_id"], r.tenant_id),
            "disconnect_adapter":     lambda r, d: a.disconnect(d["adapter_id"], r.tenant_id),
            # ── 技能管理 ──
            "list_skills":            lambda r, d: sk.list(r.tenant_id, d.get("offset", 0), d.get("limit", 20), d.get("category"), d.get("keyword")),
            "get_skill":              lambda r, d: sk.get(r.tenant_id, d["skill_id"]),
            "enable_skill":           lambda r, d: sk.enable(r.tenant_id, d["skill_id"]),
            "disable_skill":          lambda r, d: sk.disable(r.tenant_id, d["skill_id"]),
            "list_enabled_mcp_tools": lambda r, d: sk.list_enabled_mcp_tools(r.tenant_id),
            # ── 业务模板 ──
            "list_template_domains":    lambda r, d: t.list_domains(r.tenant_id, d.get("offset", 0), d.get("limit", 20)),
            "get_template_domain":      lambda r, d: t.get_domain(d["domain_id"], r.tenant_id),
            "create_template_domain":   lambda r, d: t.create_domain(r.tenant_id, d),
            "update_template_domain":   lambda r, d: t.update_domain(d["domain_id"], r.tenant_id, d),
            "delete_template_domain":   lambda r, d: _deleted(t.delete_domain(d["domain_id"], r.tenant_id)),
            "list_template_scenarios":  lambda r, d: t.list_scenarios(r.tenant_id, d.get("domain_id"), d.get("offset", 0), d.get("limit", 20)),
            "get_template_scenario":    lambda r, d: t.get_scenario(d["scenario_id"], r.tenant_id),
            "create_template_scenario": lambda r, d: t.create_scenario(r.tenant_id, d),
            "update_template_scenario": lambda r, d: t.update_scenario(d["scenario_id"], r.tenant_id, d),
            "delete_template_scenario": lambda r, d: _deleted(t.delete_scenario(d["scenario_id"], r.tenant_id)),
            "list_template_objects":    lambda r, d: t.list_objects(r.tenant_id, d["scenario_id"], d.get("offset", 0), d.get("limit", 20)),
            "create_template_object":   lambda r, d: t.create_object(r.tenant_id, d["scenario_id"], d),
            "update_template_object":   lambda r, d: t.update_object(d["object_id"], r.tenant_id, d),
            "delete_template_object":   lambda r, d: _deleted(t.delete_object(d["object_id"], r.tenant_id)),
            "list_template_relations":  lambda r, d: t.list_relations(r.tenant_id, d["scenario_id"], d.get("offset", 0), d.get("limit", 20)),
            "create_template_relation": lambda r, d: t.create_relation(r.tenant_id, d["scenario_id"], d),
            "update_template_relation": lambda r, d: t.update_relation(d["relation_id"], r.tenant_id, d),
            "delete_template_relation": lambda r, d: _deleted(t.delete_relation(d["relation_id"], r.tenant_id)),
            # ── 提示词模板 ──
            "list_prompt_templates":      lambda r, d: pt.list_templates(
                r.tenant_id, d.get("scope"), d.get("category"),
                d.get("keyword"), d.get("offset", 0), d.get("limit", 20),
                domain_space_id=d.get("domain_space_id"),
            ),
            "get_prompt_template":         lambda r, d: pt.get_template(
                d["template_id"], r.tenant_id,
                domain_space_id=d.get("domain_space_id"),
            ),
            "create_prompt_template":      lambda r, d: pt.create_template(
                r.tenant_id, d, d.get("user_id"),
                domain_space_id=d.get("domain_space_id"),
            ),
            "update_prompt_template":      lambda r, d: pt.update_template(
                d["template_id"], r.tenant_id, d, d.get("user_id"),
                domain_space_id=d.get("domain_space_id"),
            ),
            "delete_prompt_template":      lambda r, d: _deleted(
                pt.delete_template(d["template_id"], r.tenant_id,
                                   domain_space_id=d.get("domain_space_id")),
            ),
            "copy_prompt_template":        lambda r, d: pt.copy_template(
                d["template_id"], r.tenant_id, d.get("user_id"),
                domain_space_id=d.get("domain_space_id"),
            ),
            "list_prompt_template_versions": lambda r, d: pt.list_versions(
                d["template_id"], r.tenant_id,
                domain_space_id=d.get("domain_space_id"),
            ),
            "rollback_prompt_template":    lambda r, d: pt.rollback_version(
                d["template_id"], r.tenant_id, d["target_version"],
                d.get("user_id"), domain_space_id=d.get("domain_space_id"),
            ),
            "list_template_constraints":   lambda r, d: t.list_constraints(r.tenant_id, d["scenario_id"], d.get("offset", 0), d.get("limit", 20)),
            "create_template_constraint":  lambda r, d: t.create_constraint(r.tenant_id, d["scenario_id"], d),
            "update_template_constraint":  lambda r, d: t.update_constraint(d["constraint_id"], r.tenant_id, d),
            "delete_template_constraint":  lambda r, d: _deleted(t.delete_constraint(d["constraint_id"], r.tenant_id)),
            # ── 本体 YAML 导入导出 ──  # [jonex]
            "export_template_ontology_yaml": lambda r, d: t.export_ontology_yaml(r.tenant_id, d["scenario_id"]),
            "import_template_ontology_yaml": lambda r, d: t.import_ontology_yaml(
                r.tenant_id, d["scenario_id"], d["yaml_text"],
                dry_run=d.get("dry_run", True), mode=d.get("mode", "merge"),
            ),
        }

    def _build_metadata(self) -> CapabilityMetadata:
        return CapabilityMetadata(
            capability_id="business_domain",
            capability_name="业务领域与生态管理",
            capability_type=CapabilityType.BUSINESS,
            version="v1",
            description="领域空间、领域服务、引擎管理、生态适配器、Skills、业务模板",
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
            tags=["业务领域", "生态管理"],
        )

    async def validate_input(self, request: CapabilityRequest) -> bool:
        return True

    async def execute(self, request: CapabilityRequest) -> CapabilityResponse:
        action = request.payload.get("action")
        data = request.payload.get("data", {})

        handler = self._dispatch.get(action)
        if not handler:
            return CapabilityResponse.error(request.request_id, 400, f"不支持的操作: {action}")

        try:
            request.tenant_id = require_tenant(request.tenant_id)
            await _check_action_permission(request)   # ← 新增
            result = await handler(request, data)
            return CapabilityResponse.ok(request.request_id, result)
        except TenantIsolationError as e:
            return CapabilityResponse.error(request.request_id, 403, str(e))
        except JonexException as e:
            return CapabilityResponse.error(request.request_id, e.code, e.message)
        except Exception as e:
            logger.exception(f"执⾏业务领域能力失败: {e}")
            return CapabilityResponse.error(request.request_id, 500, f"执行失败: {str(e)}")


async def _deleted(coro) -> dict:
    result = await coro
    return {"deleted": result}


async def _updated(coro) -> dict:
    result = await coro
    return {"updated": result}
