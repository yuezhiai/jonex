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
from jonex_core.common.security_guards import assert_secure_jwt_secret
from jonex_core.common.tenant import require_tenant

from capabilities.business_domain.services import (
    EngineService,
    AdapterService, SkillService, TemplateService,
)
from capabilities.business_domain.services.prompt_template_service import PromptTemplateService
from capabilities.business_domain.services.template_publish_service import TemplatePublishService
from capabilities.business_domain.services.template_approval_service import TemplateApprovalService
from capabilities.business_domain.services.template_rollback_service import TemplateRollbackService

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
    "check_prompt_template_name": "prompt:read",
    "rollback_prompt_template": "prompt:write",
    "create_template_constraint": "template:write",
    "update_template_constraint": "template:write",
    "delete_template_constraint": "template:write",
    "import_template_ontology_yaml": "template:write",
    "export_template_ontology_yaml": "template:write",
    "publish_template_scenario": "template:write",
    "list_scenario_versions": "template:read",
    "get_scenario_version": "template:read",
    # 审批流：提交/撤回是编辑动作；列表是读动作；通过/驳回另在 service 内查租户管理员。
    "submit_publish_request": "template:write",
    "cancel_publish_request": "template:write",
    "approve_publish_request": "template:write",
    "reject_publish_request": "template:write",
    "list_publish_requests": "template:read",
    "rollback_scenario_version": "template:write",
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
    from jonex_core.security.permission import has_permission

    if not await has_permission(request.tenant_id, int(request.user_id), code):
        raise PermissionDeniedError(
            message=translate("err.auth.insufficient_permission", params={"required": code}, fallback=f"权限不足：需要权限 {code}")
        )


async def _require_tenant_admin_for_publish(request) -> None:
    """强制审批：直接发布只允许租户管理员（编辑走 submit_publish_request）。

    无 user_id（测试 token / 系统链路）一律拒绝 —— 发布是提权动作，不能匿名。
    """
    if not request.user_id:
        raise PermissionDeniedError(
            message="发布标准需要租户管理员；请提交发布申请",
        )
    try:
        uid = int(request.user_id)
    except (TypeError, ValueError):
        raise PermissionDeniedError(
            message="发布标准需要租户管理员；请提交发布申请",
        )
    from jonex_core.security.permission import has_permission

    if not (
        await has_permission(request.tenant_id, uid, "tenant:admin")
        or await has_permission(request.tenant_id, uid, "platform:admin")
    ):
        raise PermissionDeniedError(
            message="发布标准需要租户管理员；请提交发布申请（审批通过后生效）",
        )


class BusinessDomainCapability(BaseCapability):
    """业务领域 + 生态管理能力"""

    def __init__(self):
        self._engine = EngineService()
        self._adapter = AdapterService()
        self._skill = SkillService()
        self._template = TemplateService()
        self._prompt_template = PromptTemplateService()
        self._publish = TemplatePublishService()
        self._approval = TemplateApprovalService()
        self._rollback = TemplateRollbackService()
        self._dispatch = self._build_dispatch()
        super().__init__()

    async def initialize(self) -> None:
        # 安全守卫：JWT_SECRET 必须为真实随机值（**生产档 ENV=prod 硬拦**，非生产只告警；
        # 见 jonex_core/common/security_guards.py 约束 2）。本服务校验用户 JWT，
        # 使用公开默认值即可被伪造管理员 token。
        assert_secure_jwt_secret()
        await super().initialize()

    def register_routes(self, app):
        """**不注册任何并行 REST 层** —— 该层已于 2026-10 物理删除。

        历史背景（2026-10 渗透测试 RC-A）：business_domain 除 `/invoke`（生产链路，
        强制内部服务 JWT）外，还额外挂了一层并行 REST（`/api/v1/templates`、
        `/api/v1/prompt-templates` 等 53 条）。该层**不在生产链路上** —— Gateway 对
        business_domain 一律经 Sidecar `/invoke`（`api_gateway/routes/business_domain.py`
        的 `ecosystem_router`，其本身还带 router 级 `require_auth_header`）；却
        **无任何鉴权**且按客户端 `X-Tenant-ID` 决定租户，容器端口一旦可达即为
        未授权目录读取与跨租户写入面（报告 3.3/3.4/3.5）。

        处置过程：
        1. P1 先加 `CONTAINER_REST_ENABLED` 开关（默认 `false`）→ 生产不挂载，
           攻击面 137 → 0；
        2. 随后**物理删除**该层实现与开关本身（纯清理，生产行为不变），使「误挂载」
           这一类风险从根上消失。
        3. 原 `api/__init__.py` 里 `prompt_templates_router` 的 `prefix="/api/v1"`
           **双前缀缺陷**（与外层叠加成 `/api/v1/api/v1/prompt-templates`，报告 3.5）
           随文件删除一并消失。

        ⚠️ **不要再往这里加路由**。需要新的对外能力时：
        - 在 `_build_dispatch` 里加 invoke action（`/invoke` 注册在
          `deploy/start_capability.py`，与本方法无关）
        - 同步 `api_gateway/routes/business_domain.py` 与 Sidecar 契约
        """
        return super().register_routes(app)

    def _build_dispatch(self) -> dict:
        e = self._engine
        a = self._adapter
        sk = self._skill
        t = self._template
        pt = self._prompt_template
        p = self._publish
        ap = self._approval
        rb = self._rollback

        async def _publish_as_admin(r, publish, scenario_id):
            await _require_tenant_admin_for_publish(r)
            return await publish.publish_scenario(
                r.tenant_id, scenario_id,
                published_by=r.username or r.user_id,
            )

        async def _rollback_as_admin(r, rollback, scenario_id, version):
            await _require_tenant_admin_for_publish(r)
            return await rollback.rollback(
                r.tenant_id, scenario_id, version,
                published_by=r.username or r.user_id,
            )

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
            "list_template_domains":    lambda r, d: t.list_domains(r.tenant_id, d.get("offset", 0), d.get("limit", 20), d.get("status")),
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
            "check_prompt_template_name":  lambda r, d: pt.check_name_exists(
                r.tenant_id, d.get("name"),
                domain_space_id=d.get("domain_space_id"),
                template_id=d.get("template_id"),
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
            # ── 模板发布 / 编译预览 / 影响范围 ──
            "publish_template_scenario":     lambda r, d: _publish_as_admin(
                r, p, d["scenario_id"],
            ),
            # ── 发布审批（强制审批） ──
            "submit_publish_request":        lambda r, d: ap.submit(
                r.tenant_id, d["scenario_id"], r.user_id, r.username,
            ),
            "list_publish_requests":         lambda r, d: ap.list_requests(
                r.tenant_id, d["scenario_id"], r.user_id, r.username,
            ),
            "approve_publish_request":       lambda r, d: ap.approve(
                r.tenant_id, d["request_id"], r.user_id, r.username,
                d.get("comment"),
            ),
            "reject_publish_request":        lambda r, d: ap.reject(
                r.tenant_id, d["request_id"], r.user_id, r.username,
                d.get("comment"),
            ),
            "cancel_publish_request":        lambda r, d: ap.cancel(
                r.tenant_id, d["request_id"], r.user_id, r.username,
            ),
            "rollback_scenario_version":     lambda r, d: _rollback_as_admin(
                r, rb, d["scenario_id"], d.get("version"),
            ),
            "list_scenario_versions":        lambda r, d: p.list_versions(
                r.tenant_id, d.get("scenario_id", ""),
                d.get("offset", 0), d.get("limit", 20),
            ),
            "get_scenario_version":          lambda r, d: p.get_version(
                r.tenant_id, d.get("scenario_id", ""), d.get("version", 0),
            ),
            "preview_compiled_schema":       lambda r, d: p.compile_preview(r.tenant_id, d["scenario_id"]),
            "list_impacted_knowledge_bases": lambda r, d: p.list_impacted_kbs(r.tenant_id, d["scenario_id"]),
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
