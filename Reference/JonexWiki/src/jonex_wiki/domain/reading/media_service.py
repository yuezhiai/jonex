"""Read an immutable Wiki image with the existing Bundle manifest checks."""
import base64
import hashlib
import json
import re

from jonex_wiki.kernel.errors import NotFoundError, ValidationError


def read_media(view, asset_id, document_id, include_content=False):
    if not re.fullmatch(r'image_[0-9a-f]{32}', asset_id or '') or not document_id:
        raise ValidationError('无效的图片身份')
    if 'query/media_catalog.json' not in view.list_entries():
        raise NotFoundError('该发布版本没有图片目录，请重新编译')
    catalog = json.loads(view.read_bytes('query/media_catalog.json'))
    asset = next((a for a in catalog.get('assets', []) if a['asset_id'] == asset_id and a['document_id'] == document_id), None)
    if asset is None:
        raise NotFoundError('图片不属于该文档或发布版本')
    if not re.fullmatch(r'media/assets/[0-9a-f]{64}\.(?:jpg|jpeg|png|webp)', asset['path']):
        raise ValidationError('图片路径无效')
    if asset.get('mime_type') not in ('image/jpeg', 'image/png', 'image/webp'):
        raise ValidationError('图片类型无效')
    if not include_content:
        return {**asset, 'bundle_sha': view.manifest_hash}
    data = view.read_bytes(asset['path'])
    if len(data) > 8 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != asset['sha256']:
        raise ValidationError('图片内容身份不匹配')
    return {'asset_id': asset_id, 'document_id': document_id, 'bundle_sha': view.manifest_hash,
            'mime_type': asset['mime_type'], 'sha256': asset['sha256'], 'content_base64': base64.b64encode(data).decode('ascii')}
