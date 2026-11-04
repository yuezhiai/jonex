from fastapi import APIRouter, Depends, Request
from jonex_wiki.api.deps import require_tenant
from jonex_wiki.kernel.errors import ValidationError

router = APIRouter(prefix="/api/v1/kbs/{kb_id}/scenarios", tags=["scenarios"])


@router.get("")
async def list_scenarios(
    kb_id: str, request: Request, tenant_id: str = Depends(require_tenant)
):
    return await request.app.state.app.scenario_service().list(tenant_id, kb_id)


@router.put("/{scenario_id}")
async def configure_scenario(
    kb_id: str,
    scenario_id: str,
    request: Request,
    tenant_id: str = Depends(require_tenant),
):
    body = await request.json()
    if not isinstance(body, dict):
        raise ValidationError("SCENARIO_OPTIONS_INVALID")
    return await request.app.state.app.scenario_service().configure(
        tenant_id, kb_id, scenario_id, body, request.headers.get("X-Jonex-User-ID", "")
    )


@router.post("/{scenario_id}/{operation}")
async def scenario_operation(
    kb_id: str,
    scenario_id: str,
    operation: str,
    request: Request,
    tenant_id: str = Depends(require_tenant),
):
    body = await request.json()
    if not isinstance(body, dict):
        raise ValidationError("SCENARIO_OPTIONS_INVALID")
    return await request.app.state.app.scenario_service().operation(
        tenant_id,
        kb_id,
        scenario_id,
        operation,
        body,
        request.headers.get("X-Jonex-User-ID", ""),
    )
