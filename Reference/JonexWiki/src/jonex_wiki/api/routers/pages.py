"""平台集成:只读页面读取端点(从已发布 bundle 读 Wiki 页面树/单页/反向链接/claim)。

bundle 布局:``compile/published`` symlink → ``versions/{bundle_sha}``,内含
``query/navigation.json``(page_index/page_graph)、``indexes/links.json``、
``claims.jsonl`` 与 ``wiki/{page_path}``。本模块只读,不改动 bundle。

读取语义全部下沉到 ``WikiApplication``(见 ``jonex_wiki.domain.reading``),
本模块只做 HTTP 投影。
"""

from fastapi import APIRouter, Depends, Query, Request

from jonex_wiki.api.deps import require_tenant

router = APIRouter(prefix="/api/v1/kbs/{kb_id}", tags=["pages"])

@router.get('/images/{asset_id}')
async def image_asset(kb_id: str, asset_id: str, revision: str, document_id: str,
                      request: Request, variant: str = 'preview', tenant_id: str = Depends(require_tenant)):
    from jonex_wiki.domain.reading.image_stream import image_response
    return await image_response(request.app.state.app.layout, tenant_id, kb_id, revision,
        asset_id, document_id, variant, request.headers)


@router.get("/resources/media/{object_id}")
async def resource_media(kb_id: str, object_id: str, revision: str, request: Request,
                         tenant_id: str = Depends(require_tenant)):
    from jonex_wiki.domain.reading.resource_stream import media_response
    return await media_response(request.app.state.app.layout, tenant_id, kb_id, revision, object_id, request.headers)


@router.post("/resources/{operation}")
async def resources(kb_id: str, operation: str, request: Request,
                    tenant_id: str = Depends(require_tenant)):
    from jonex_wiki.kernel.errors import ValidationError
    payload = await request.json()
    if not isinstance(payload, dict):
        raise ValidationError("RESOURCE_OPTIONS_MUST_BE_OBJECT")
    return await request.app.state.app.query_resources(tenant_id, kb_id, operation, payload,
        actor=request.headers.get("X-Jonex-User-ID", ""))


@router.get("/graph/{operation}")
async def graph_read(kb_id: str, operation: str, request: Request,
                     tenant_id: str = Depends(require_tenant), graph_revision: str = "",
                     query: str = "", node_type: str = "", node_id: str = "",
                     edge_id: str = "", evidence_id: str = "", document_id: str = "",
                     include_content: bool = False, asset_id: str = "",
                     direction: str = "both", families: list[str] | None = Query(None),
                     limit: int = Query(20, ge=1, le=100), cursor: str = ""):
    from jonex_wiki.domain.reading.graph_service import QUERY_FIELDS
    from jonex_wiki.kernel.errors import ValidationError
    if operation not in QUERY_FIELDS:
        raise ValidationError("未知图查询操作")
    values = locals()
    options = {key: values[key] for key in QUERY_FIELDS[operation] if values.get(key) is not None}
    return await request.app.state.app.query_graph(tenant_id, kb_id, operation, **options)


@router.get("/pages")
async def list_pages(kb_id: str, request: Request,
                     tenant_id: str = Depends(require_tenant)):
    """已发布 Wiki 的阅读树页面清单(完整 PageIndexEntry 元数据 + type)。"""
    return await request.app.state.app.list_pages(tenant_id, kb_id)


@router.get("/pages/{page_id:path}/backlinks")
async def get_page_backlinks(kb_id: str, page_id: str, request: Request,
                             tenant_id: str = Depends(require_tenant)):
    """「链入本页」:反向链接 + 正文提及(编译期索引)。"""
    return await request.app.state.app.get_page_backlinks(tenant_id, kb_id, page_id)


@router.get("/pages/{page_id:path}/claims")
async def get_page_claims(kb_id: str, page_id: str, request: Request,
                          tenant_id: str = Depends(require_tenant)):
    """页面级 claim 溯源明细。"""
    return await request.app.state.app.get_page_claims(tenant_id, kb_id, path=page_id)


@router.get("/pages/{page_id:path}")
async def get_page(kb_id: str, page_id: str, request: Request,
                   tenant_id: str = Depends(require_tenant)):
    """读取单页(结构化 index/page/body/content 别名/references)。"""
    return await request.app.state.app.read_page(tenant_id, kb_id, page_id)


@router.get("/claims")
async def get_claims(kb_id: str, request: Request, document_id: str = "",
                     tenant_id: str = Depends(require_tenant)):
    """按源文档过滤的 claim 溯源明细。"""
    return await request.app.state.app.get_page_claims(
        tenant_id, kb_id, document_id=document_id
    )
