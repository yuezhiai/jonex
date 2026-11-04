from fastapi import APIRouter, Depends, Request
from jonex_wiki.api.deps import require_tenant
from jonex_wiki.domain.reading.resource_stream import media_response

router = APIRouter()

@router.get("/images/{kb_id}/{asset_id}")
async def read_image(kb_id: str, asset_id: str, revision: str, document_id: str,
                     request: Request, variant: str = "preview",
                     tenant_id: str = Depends(require_tenant)):
    from jonex_wiki.domain.reading.image_stream import image_response
    return await image_response(request.app.state.app.layout, tenant_id, kb_id, revision,
        asset_id, document_id, variant, request.headers)

@router.get("/media/{kb_id}/{object_id}")
async def read_media(kb_id: str, object_id: str, revision: str, request: Request,
                     tenant_id: str = Depends(require_tenant)):
    return await media_response(request.app.state.app.layout, tenant_id, kb_id, revision, object_id, request.headers)
