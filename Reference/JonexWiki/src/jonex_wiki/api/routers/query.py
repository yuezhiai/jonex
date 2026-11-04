from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from typing import Literal

from jonex_wiki.api.deps import require_tenant
from jonex_wiki.kernel.query_images import QueryImageContext

router = APIRouter(prefix="/api/v1/kbs/{kb_id}", tags=["query"])


class QueryBody(BaseModel):
    model_config = {"extra": "forbid"}
    question: str
    thinking_mode: Literal["fast", "standard", "deep"] = "fast"
    retrieval_strategy: Literal["tree", "direct", "structure", "semantic"] | None = None
    query_context: QueryImageContext | None = None


@router.post("/query", status_code=202)
async def query(kb_id: str, body: QueryBody, request: Request,
                tenant_id: str = Depends(require_tenant)):
    options = {"thinking_mode": body.thinking_mode}
    if body.retrieval_strategy is not None:
        options["retrieval_strategy"] = body.retrieval_strategy
    if body.query_context is not None:
        options['query_context'] = body.query_context.model_dump(mode='json')
    return await request.app.state.app.query(tenant_id, kb_id, body.question, **options)
