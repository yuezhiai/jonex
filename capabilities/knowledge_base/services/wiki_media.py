"""Stable Wiki asset identity with an expiring document-scoped viewing URL."""
from urllib.parse import urlencode
import re
from jonex_core.common.crypto import generate_view_token


from jonex_core.common.wiki_images import image_url, verify_image_ticket


def attach_image_urls(images: list, tenant_id: str, user_id: str, kb_id: str, revision: str):
    for image in images:
        if image.get('asset_id') and image.get('document_id'):
            for field, variant in [('preview_url', 'preview'), ('display_url', 'display'), ('original_url', 'original')]:
                image[field] = image_url(tenant_id, user_id, kb_id, image['document_id'], revision, image['asset_id'], variant)
    return images


def media_url(tenant_id: str, kb_id: str, document_id: str, bundle_sha: str, asset_id: str) -> str:
    return '/api/v1/knowledge-base/wiki/media/' + asset_id + '?' + urlencode({
        'knowledge_base_id': kb_id, 'document_id': document_id, 'graph_revision': bundle_sha,
        'token': generate_view_token(tenant_id, document_id, 300)})


def resource_media_url(tenant_id: str, kb_id: str, document_id: str, revision: str, object_id: str) -> str:
    return '/api/v1/knowledge-base/wiki/resource-media/' + object_id + '?' + urlencode({
        'knowledge_base_id': kb_id, 'document_id': document_id, 'revision': revision,
        'token': generate_view_token(tenant_id, document_id, 300)})


def image_location(asset: dict, document_id: str, bundle_sha: str) -> dict | None:
    """Project a verified, pinned asset without requiring domain metadata.

    Membership and byte hashes belong to the engine media reader. This boundary
    additionally checks owner/version identity and optional physical page shape.
    """
    if (not isinstance(asset, dict) or not isinstance(bundle_sha, str)
            or not re.fullmatch(r'[0-9a-f]{64}', bundle_sha)
            or asset.get('document_id') != document_id
            or asset.get('bundle_sha') != bundle_sha):
        return None
    index, asset_id = asset.get('image_idx'), asset.get('asset_id')
    if (type(index) is not int or index < 0 or not isinstance(asset_id, str)
            or not re.fullmatch(r'image_[0-9a-f]{32}', asset_id)):
        return None
    page = asset.get('pdf_page')
    if page is not None and (type(page) is not int or page < 1):
        return None
    relative = str(asset.get('path') or '')
    ext = relative.rsplit('.', 1)[-1].lower() if '.' in relative else None
    if ext not in {'png', 'jpg', 'jpeg', 'webp'}:
        return None
    return {'type': 'image', 'image_idx': index, 'asset_id': asset_id,
        'bundle_sha': bundle_sha, 'page_no': page - 1 if page is not None else None,
        'asset_ext': ext,
        'text': str(asset.get('caption') or asset.get('label') or f'来源图片 {index + 1}')[:1000]}
