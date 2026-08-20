"""
生态管理 API 路由（薄接入层）

通过 Sidecar 调用对应能力：
- 生态（适配器/技能/模板/引擎）-> business.business_domain.v1

注：领域空间 / 领域服务（business.knowledge_base.v1）已迁移到
    api_gateway/routes/knowledge_base.py（/api/v1/knowledge-base/spaces|services）。
"""
import os
from typing import Optional

import jwt
from fastapi import APIRouter, Body, Depends, File, Query, Request, UploadFile

from jonex_core.common import (
    CapabilityInvokeError,
    CapabilityTimeoutError,
    JonexException,
    ServiceUnavailableError,
    get_config,
    get_logger,
    success_response,
    transmit_locale_header,
)
from jonex_core.common.tenant import extract_tenant_id
from jonex_core.common.i18n import translate
from api_gateway.deps import require_auth_header, raise_from_capability_result

logger = get_logger("api_business_domain")

ecosystem_router = APIRouter(prefix="/api/v1/ecosystem", dependencies=[Depends(require_auth_header)])

def _extract_user_id(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    config = get_config()
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


def _extract_username(request: Request) -> str | None:
    """从请求 Authorization 头的 JWT 中提取 username。"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    config = get_config()
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
        return payload.get("username") or None
    except jwt.PyJWTError:
        return None


async def _call_bd_capability(request: Request, action: str, payload: dict):
    import asyncio
    import random

    import httpx

    config = get_config()
    sidecar_url = config.SIDECAR_URL
    request_id = getattr(request.state, "request_id", "")
    tenant_id = extract_tenant_id(request)
    user_id = _extract_user_id(request)
    username = _extract_username(request)

    sidecar_payload = {
        "capability_id": "business.business_domain.v1",
        "payload": {"action": action, "data": payload},
        "tenant_id": tenant_id,
    }
    if user_id:
        sidecar_payload["user_id"] = user_id
    if username:
        sidecar_payload["username"] = username

    headers = {
        "X-API-Key": "jonex_test_gateway",
        "X-Request-ID": request_id,
        "X-Tenant-ID": tenant_id,
        "X-Forwarded-For": request.client.host if request.client else "",
    }
    transmit_locale_header(headers)

    # Sidecar 是本地容器，重启通常在 1-3s 内完成。
    # 瞬态连接错误（Connection refused / DNS 解析失败 / 连接重置）
    # 做指数退避 + 全抖动重试，其余错误直抛不重试。
    max_retries = int(os.getenv("GATEWAY_SIDECAR_RETRIES", "2"))
    base_delay = float(os.getenv("GATEWAY_SIDECAR_BASE_DELAY", "1.0"))
    max_delay = float(os.getenv("GATEWAY_SIDECAR_MAX_DELAY", "8.0"))
    timeout = float(os.getenv("GATEWAY_SIDECAR_TIMEOUT", "120"))

    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{sidecar_url}/invoke", json=sidecar_payload, headers=headers,
                )
                result = response.json()
                if response.status_code != 200 or not result.get("success", False):
                    raise_from_capability_result(result)
                return result.get("data", {})
        except httpx.TimeoutException:
            raise CapabilityTimeoutError()
        except httpx.TransportError as e:
            last_exc = e
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                jitter = random.uniform(0, delay)
                logger.warning(
                    f"Sidecar 调用 {action} 瞬断 (attempt {attempt+1}/{max_retries+1})，"
                    f"{jitter:.1f}s 后重试: {e}",
                )
                await asyncio.sleep(jitter)
            else:
                logger.error(
                    f"Sidecar 调用 {action} 失败，已重试 {max_retries} 次: {e}",
                )
                raise ServiceUnavailableError(details={"action": action})
        except JonexException:
            raise
        except Exception as e:
            logger.error(f"业务领域调用异常: action={action}, error={e}")
            raise ServiceUnavailableError(details={"action": action})


# ════════════════════════════════════════════════════════════
# 生态适配器 /api/v1/ecosystem/adapters  ->  business.business_domain.v1
# ════════════════════════════════════════════════════════════

@ecosystem_router.get("/adapters", summary="获取适配器列表")
async def list_adapters(
    request: Request,
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取生态适配器分页列表"""
    result = await _call_bd_capability(request, "list_adapters", {"offset": offset, "limit": limit})
    return success_response(data=result)


@ecosystem_router.post("/adapters", summary="创建适配器")
async def create_adapter(request: Request, payload: dict = Body(...)):
    """创建新的生态适配器"""
    result = await _call_bd_capability(request, "create_adapter", payload)
    return success_response(data=result)


@ecosystem_router.patch("/adapters/{adapter_id}", summary="更新适配器")
async def update_adapter(request: Request, adapter_id: str, payload: dict = Body(...)):
    """更新指定生态适配器的配置"""
    payload["adapter_id"] = adapter_id
    result = await _call_bd_capability(request, "update_adapter", payload)
    return success_response(data=result)


@ecosystem_router.post("/adapters/{adapter_id}/connect", summary="连接适配器")
async def connect_adapter(request: Request, adapter_id: str):
    """建立适配器连接"""
    result = await _call_bd_capability(request, "connect_adapter", {"adapter_id": adapter_id})
    return success_response(data=result)


@ecosystem_router.post("/adapters/{adapter_id}/disconnect", summary="断开适配器")
async def disconnect_adapter(request: Request, adapter_id: str):
    """断开适配器连接"""
    result = await _call_bd_capability(request, "disconnect_adapter", {"adapter_id": adapter_id})
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 技能管理 /api/v1/ecosystem/skills
# ════════════════════════════════════════════════════════════

@ecosystem_router.get("/skills", summary="获取技能列表")
async def list_skills(
    request: Request,
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
    category: str | None = Query(None, description="技能分类过滤"),
    keyword: str | None = Query(None, description="搜索关键词"),
):
    """获取生态技能分页列表"""
    result = await _call_bd_capability(request, "list_skills", {
        "offset": offset, "limit": limit, "category": category, "keyword": keyword,
    })
    return success_response(data=result)


@ecosystem_router.get("/skills/{skill_id}", summary="获取技能详情")
async def get_skill(request: Request, skill_id: str):
    """获取指定技能的详细信息"""
    result = await _call_bd_capability(request, "get_skill", {"skill_id": skill_id})
    return success_response(data=result)


@ecosystem_router.post("/skills/{skill_id}/enable", summary="启用技能")
async def enable_skill(request: Request, skill_id: str):
    """启用指定技能"""
    result = await _call_bd_capability(request, "enable_skill", {"skill_id": skill_id})
    return success_response(data=result)


@ecosystem_router.post("/skills/{skill_id}/disable", summary="禁用技能")
async def disable_skill(request: Request, skill_id: str):
    """禁用指定技能"""
    result = await _call_bd_capability(request, "disable_skill", {"skill_id": skill_id})
    return success_response(data=result)


@ecosystem_router.get("/skills/mcp-tools", summary="获取已启用 MCP 工具列表")
async def list_enabled_mcp_tools(request: Request):
    """获取当前租户已启用的 MCP 工具列表"""
    result = await _call_bd_capability(request, "list_enabled_mcp_tools", {})
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 业务领域模板 /api/v1/ecosystem/templates
# ════════════════════════════════════════════════════════════

@ecosystem_router.get("/templates/domains", summary="获取模板领域列表")
async def list_template_domains(
    request: Request,
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取业务模板领域分页列表"""
    result = await _call_bd_capability(request, "list_template_domains", {"offset": offset, "limit": limit})
    return success_response(data=result)


@ecosystem_router.post("/templates/domains", summary="创建模板领域")
async def create_template_domain(request: Request, payload: dict = Body(...)):
    """创建新的业务模板领域"""
    result = await _call_bd_capability(request, "create_template_domain", payload)
    return success_response(data=result)


@ecosystem_router.get("/templates/domains/{domain_id}", summary="获取模板领域详情")
async def get_template_domain(request: Request, domain_id: str):
    """获取指定模板领域的详细信息"""
    result = await _call_bd_capability(request, "get_template_domain", {"domain_id": domain_id})
    return success_response(data=result)


@ecosystem_router.patch("/templates/domains/{domain_id}", summary="更新模板领域")
async def update_template_domain(request: Request, domain_id: str, payload: dict = Body(...)):
    """更新指定模板领域的配置"""
    payload["domain_id"] = domain_id
    result = await _call_bd_capability(request, "update_template_domain", payload)
    return success_response(data=result)


@ecosystem_router.delete("/templates/domains/{domain_id}", summary="删除模板领域")
async def delete_template_domain(request: Request, domain_id: str):
    """删除指定模板领域"""
    result = await _call_bd_capability(request, "delete_template_domain", {"domain_id": domain_id})
    return success_response(data=result)


@ecosystem_router.get("/templates/scenarios", summary="获取模板场景列表")
async def list_template_scenarios(
    request: Request,
    domain_id: Optional[str] = Query(None, description="领域 ID 过滤"),
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取业务模板场景分页列表"""
    result = await _call_bd_capability(
        request,
        "list_template_scenarios",
        {"domain_id": domain_id, "offset": offset, "limit": limit},
    )
    return success_response(data=result)


@ecosystem_router.post("/templates/scenarios", summary="创建模板场景")
async def create_template_scenario(request: Request, payload: dict = Body(...)):
    """创建新的业务模板场景"""
    result = await _call_bd_capability(request, "create_template_scenario", payload)
    return success_response(data=result)


@ecosystem_router.get("/templates/scenarios/{scenario_id}", summary="获取模板场景详情")
async def get_template_scenario(request: Request, scenario_id: str):
    """获取指定模板场景的详细信息"""
    result = await _call_bd_capability(request, "get_template_scenario", {"scenario_id": scenario_id})
    return success_response(data=result)


@ecosystem_router.patch("/templates/scenarios/{scenario_id}", summary="更新模板场景")
async def update_template_scenario(request: Request, scenario_id: str, payload: dict = Body(...)):
    """更新指定模板场景的配置"""
    payload["scenario_id"] = scenario_id
    result = await _call_bd_capability(request, "update_template_scenario", payload)
    return success_response(data=result)


@ecosystem_router.delete("/templates/scenarios/{scenario_id}", summary="删除模板场景")
async def delete_template_scenario(request: Request, scenario_id: str):
    """删除指定模板场景"""
    result = await _call_bd_capability(request, "delete_template_scenario", {"scenario_id": scenario_id})
    return success_response(data=result)


@ecosystem_router.get("/templates/scenarios/{scenario_id}/objects", summary="获取模板实体对象列表")
async def list_template_objects(
    request: Request,
    scenario_id: str,
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取指定场景下的实体对象分页列表"""
    result = await _call_bd_capability(
        request,
        "list_template_objects",
        {"scenario_id": scenario_id, "offset": offset, "limit": limit},
    )
    return success_response(data=result)


@ecosystem_router.post("/templates/scenarios/{scenario_id}/objects", summary="创建模板实体对象")
async def create_template_object(request: Request, scenario_id: str, payload: dict = Body(...)):
    """在指定场景下创建实体对象"""
    payload["scenario_id"] = scenario_id
    result = await _call_bd_capability(request, "create_template_object", payload)
    return success_response(data=result)


@ecosystem_router.patch("/templates/objects/{object_id}", summary="更新模板实体对象")
async def update_template_object(request: Request, object_id: str, payload: dict = Body(...)):
    """更新指定实体对象的配置"""
    payload["object_id"] = object_id
    result = await _call_bd_capability(request, "update_template_object", payload)
    return success_response(data=result)


@ecosystem_router.delete("/templates/objects/{object_id}", summary="删除模板实体对象")
async def delete_template_object(request: Request, object_id: str):
    """删除指定实体对象"""
    result = await _call_bd_capability(request, "delete_template_object", {"object_id": object_id})
    return success_response(data=result)


@ecosystem_router.get("/templates/scenarios/{scenario_id}/relations", summary="获取模板关系列表")
async def list_template_relations(
    request: Request,
    scenario_id: str,
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取指定场景下的关系分页列表"""
    result = await _call_bd_capability(
        request,
        "list_template_relations",
        {"scenario_id": scenario_id, "offset": offset, "limit": limit},
    )
    return success_response(data=result)


@ecosystem_router.post("/templates/scenarios/{scenario_id}/relations", summary="创建模板关系")
async def create_template_relation(request: Request, scenario_id: str, payload: dict = Body(...)):
    """在指定场景下创建关系"""
    payload["scenario_id"] = scenario_id
    result = await _call_bd_capability(request, "create_template_relation", payload)
    return success_response(data=result)


@ecosystem_router.patch("/templates/relations/{relation_id}", summary="更新模板关系")
async def update_template_relation(request: Request, relation_id: str, payload: dict = Body(...)):
    """更新指定关系的配置"""
    payload["relation_id"] = relation_id
    result = await _call_bd_capability(request, "update_template_relation", payload)
    return success_response(data=result)


@ecosystem_router.delete("/templates/relations/{relation_id}", summary="删除模板关系")
async def delete_template_relation(request: Request, relation_id: str):
    """删除指定关系"""
    result = await _call_bd_capability(request, "delete_template_relation", {"relation_id": relation_id})
    return success_response(data=result)


# ── 模板约束 ──────────────────────────────────────────────

@ecosystem_router.get("/templates/scenarios/{scenario_id}/constraints", summary="获取模板约束列表")
async def list_template_constraints(request: Request, scenario_id: str, offset: int = 0, limit: int = 20):
    result = await _call_bd_capability(
        request, "list_template_constraints",
        {"scenario_id": scenario_id, "offset": offset, "limit": limit},
    )
    return success_response(data=result)


@ecosystem_router.post("/templates/scenarios/{scenario_id}/constraints", summary="创建模板约束")
async def create_template_constraint(request: Request, scenario_id: str, payload: dict = Body(...)):
    payload["scenario_id"] = scenario_id
    result = await _call_bd_capability(request, "create_template_constraint", payload)
    return success_response(data=result)


@ecosystem_router.patch("/templates/constraints/{constraint_id}", summary="更新模板约束")
async def update_template_constraint(request: Request, constraint_id: str, payload: dict = Body(...)):
    payload["constraint_id"] = constraint_id
    result = await _call_bd_capability(request, "update_template_constraint", payload)
    return success_response(data=result)


@ecosystem_router.delete("/templates/constraints/{constraint_id}", summary="删除模板约束")
async def delete_template_constraint(request: Request, constraint_id: str):
    result = await _call_bd_capability(request, "delete_template_constraint", {"constraint_id": constraint_id})
    return success_response(data=result)


# ── 本体 YAML 导入导出 ──────────────────────────────────────  # [jonex]

@ecosystem_router.get(
    "/templates/scenarios/{scenario_id}/ontology-yaml/export",
    summary="导出场景本体 YAML",
)
async def export_template_ontology_yaml(request: Request, scenario_id: str):
    """导出模板场景的实体、属性、关系、约束为本体 YAML 格式。"""
    result = await _call_bd_capability(
        request, "export_template_ontology_yaml",
        {"scenario_id": scenario_id},
    )
    return success_response(data=result)


@ecosystem_router.post(
    "/templates/scenarios/{scenario_id}/ontology-yaml/import",
    summary="导入场景本体 YAML",
)
async def import_template_ontology_yaml(
    request: Request,
    scenario_id: str,
    file: UploadFile = File(...),
    dry_run: bool = Query(True, description="为 true 时仅返回变更摘要，不写入数据"),
    mode: str = Query("merge", description="导入模式：merge"),
):
    """从上传的 YAML 文件导入实体、属性、关系、约束到模板场景。

    dry_run=true 时仅校验并返回变更摘要，不实际写入。
    """
    # [jonex] 在 Gateway 读取文件内容，作为字符串传入 invoke payload，不传递文件对象
    raw = await file.read()
    try:
        yaml_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        yaml_text = raw.decode("utf-8-sig", errors="replace")

    result = await _call_bd_capability(
        request, "import_template_ontology_yaml",
        {
            "scenario_id": scenario_id,
            "yaml_text": yaml_text,
            "dry_run": dry_run,
            "mode": mode,
        },
    )
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 业务商场 /api/v1/ecosystem/marketplace
# ════════════════════════════════════════════════════════════

@ecosystem_router.get("/marketplace", summary="获取业务商场列表")
async def list_marketplace(
    request: Request,
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取已发布的业务模板商场列表"""
    result = await _call_bd_capability(request, "list_template_domains", {"offset": offset, "limit": limit, "status": "published"})
    return success_response(data=result)


@ecosystem_router.get("/marketplace/{module_id}", summary="获取商场模块详情")
async def get_marketplace_module(request: Request, module_id: str):
    """获取商场中指定模块的详细信息"""
    result = await _call_bd_capability(request, "get_template_domain", {"domain_id": module_id})
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 引擎管理 /api/v1/ecosystem/*
# ════════════════════════════════════════════════════════════

@ecosystem_router.get("/model-providers", summary="获取模型供应商列表")
async def list_model_providers(
    request: Request,
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取模型供应商分页列表"""
    result = await _call_bd_capability(request, "list_providers", {"offset": offset, "limit": limit})
    return success_response(data=result)


@ecosystem_router.post("/model-providers", summary="创建模型供应商")
async def create_model_provider(request: Request, payload: dict = Body(...)):
    """创建新的模型供应商"""
    result = await _call_bd_capability(request, "create_provider", payload)
    return success_response(data=result)


@ecosystem_router.patch("/model-providers/{provider_id}", summary="更新模型供应商")
async def update_model_provider(request: Request, provider_id: str, payload: dict = Body(...)):
    """更新指定模型供应商的配置"""
    payload["provider_id"] = provider_id
    result = await _call_bd_capability(request, "update_provider", payload)
    return success_response(data=result)


@ecosystem_router.post("/model-providers/{provider_id}/test", summary="测试模型供应商连接")
async def test_model_provider(request: Request, provider_id: str):
    """测试指定模型供应商的连接是否正常"""
    result = await _call_bd_capability(request, "test_provider", {"provider_id": provider_id})
    return success_response(data=result)


@ecosystem_router.get("/parser-configs", summary="获取解析器配置列表")
async def list_parser_configs(
    request: Request,
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取文档解析器配置分页列表"""
    result = await _call_bd_capability(request, "list_parsers", {"offset": offset, "limit": limit})
    return success_response(data=result)


@ecosystem_router.post("/parser-configs", summary="创建解析器配置")
async def create_parser_config(request: Request, payload: dict = Body(...)):
    """创建新的文档解析器配置"""
    result = await _call_bd_capability(request, "create_parser", payload)
    return success_response(data=result)


@ecosystem_router.patch("/parser-configs/{parser_id}", summary="更新解析器配置")
async def update_parser_config(request: Request, parser_id: str, payload: dict = Body(...)):
    """更新指定解析器的配置"""
    payload["parser_id"] = parser_id
    result = await _call_bd_capability(request, "update_parser", payload)
    return success_response(data=result)


@ecosystem_router.get("/data-access-methods", summary="获取数据接入方式列表")
async def list_access_methods(
    request: Request,
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取数据接入方式分页列表"""
    result = await _call_bd_capability(request, "list_access_methods", {"offset": offset, "limit": limit})
    return success_response(data=result)


@ecosystem_router.post("/data-access-methods", summary="创建数据接入方式")
async def create_access_method(request: Request, payload: dict = Body(...)):
    """创建新的数据接入方式"""
    result = await _call_bd_capability(request, "create_access_method", payload)
    return success_response(data=result)


@ecosystem_router.patch("/data-access-methods/{method_id}", summary="更新数据接入方式")
async def update_access_method(request: Request, method_id: str, payload: dict = Body(...)):
    """更新指定数据接入方式"""
    payload["method_id"] = method_id
    result = await _call_bd_capability(request, "update_access_method", payload)
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 提示词模板 /api/v1/ecosystem/prompt-templates
# ════════════════════════════════════════════════════════════


@ecosystem_router.get("/prompt-templates", summary="获取提示词模板列表")
async def list_prompt_templates(
    request: Request,
    scope: Optional[str] = Query(None, description="system | domain"),
    category: Optional[str] = Query(None, description="分类筛选"),
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    domain_space_id: Optional[str] = Query(None, description="领域空间 ID（domain 模板按空间过滤）"),
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取提示词模板分页列表。scope=system 跨租户可见，scope=domain 按租户 + 可选空间隔离。"""
    result = await _call_bd_capability(request, "list_prompt_templates", {
        "scope": scope, "category": category, "keyword": keyword,
        "domain_space_id": domain_space_id,
        "offset": offset, "limit": limit,
    })
    return success_response(data=result)


@ecosystem_router.post("/prompt-templates", summary="创建提示词模板")
async def create_prompt_template(request: Request, payload: dict = Body(...)):
    """创建领域空间提示词模板（scope 固定 domain）"""
    result = await _call_bd_capability(request, "create_prompt_template", payload)
    return success_response(data=result)


@ecosystem_router.get("/prompt-templates/{template_id}", summary="获取提示词模板详情")
async def get_prompt_template(
    request: Request, template_id: str,
    domain_space_id: str | None = Query(None, description="领域空间 ID（用于空间隔离校验）"),
):
    """获取指定提示词模板的详细信息"""
    payload = {"template_id": template_id}
    if domain_space_id:
        payload["domain_space_id"] = domain_space_id
    result = await _call_bd_capability(request, "get_prompt_template", payload)
    return success_response(data=result)


@ecosystem_router.patch("/prompt-templates/{template_id}", summary="更新提示词模板")
async def update_prompt_template(request: Request, template_id: str, payload: dict = Body(...)):
    """更新指定领域模板的配置（内容变化自动生成新版本）"""
    payload["template_id"] = template_id
    result = await _call_bd_capability(request, "update_prompt_template", payload)
    return success_response(data=result)


@ecosystem_router.delete("/prompt-templates/{template_id}", summary="删除提示词模板")
async def delete_prompt_template(
    request: Request, template_id: str,
    domain_space_id: str | None = Query(None, description="领域空间 ID（用于空间隔离校验）"),
):
    """删除指定领域模板（系统模板不可删除）"""
    payload = {"template_id": template_id}
    if domain_space_id:
        payload["domain_space_id"] = domain_space_id
    result = await _call_bd_capability(request, "delete_prompt_template", payload)
    return success_response(data=result)


@ecosystem_router.post("/prompt-templates/{template_id}/copy", summary="复制提示词模板")
async def copy_prompt_template(request: Request, template_id: str, payload: dict = Body({})):
    """复制指定模板到当前租户（system 模板复制后变为 domain 模板）"""
    payload["template_id"] = template_id
    result = await _call_bd_capability(request, "copy_prompt_template", payload)
    return success_response(data=result)


@ecosystem_router.get("/prompt-templates/{template_id}/versions", summary="获取版本历史")
async def list_prompt_template_versions(
    request: Request, template_id: str,
    domain_space_id: str | None = Query(None, description="领域空间 ID（用于空间隔离校验）"),
):
    """获取指定模板的版本历史列表"""
    payload = {"template_id": template_id}
    if domain_space_id:
        payload["domain_space_id"] = domain_space_id
    result = await _call_bd_capability(request, "list_prompt_template_versions", payload)
    return success_response(data=result)


@ecosystem_router.post("/prompt-templates/{template_id}/versions/rollback", summary="回滚版本")
async def rollback_prompt_template(
    request: Request, template_id: str, payload: dict = Body(...),
    domain_space_id: str | None = Query(None, description="领域空间 ID（用于空间隔离校验）"),
):
    """回滚到指定历史版本（生成新版本号，保留历史）"""
    payload["template_id"] = template_id
    if domain_space_id:
        payload["domain_space_id"] = domain_space_id
    result = await _call_bd_capability(request, "rollback_prompt_template", payload)
    return success_response(data=result)
