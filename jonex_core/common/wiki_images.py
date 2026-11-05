"""Actor- and revision-scoped picture tickets shared by Gateway and KB service."""
from urllib.parse import urlencode


def image_url(tenant_id: str, user_id: str, kb_id: str, document_id: str,
              revision: str, asset_id: str, variant: str = "preview", ttl: int = 900) -> str:
    """A picture ticket is bound to the actor and every storage identity field."""
    import time
    import jwt
    from jonex_core.common import get_config
    if not user_id or variant not in {"preview", "display", "original"}:
        return ""
    config = get_config()
    token = jwt.encode({"scope": "wiki_image_view_v1", "tid": tenant_id, "uid": user_id,
        "kb": kb_id, "doc": document_id, "rev": revision, "asset": asset_id,
        "variant": variant, "exp": int(time.time()) + ttl}, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)
    return '/api/v1/knowledge-base/wiki/images/' + asset_id + '?' + urlencode({
        'knowledge_base_id': kb_id, 'document_id': document_id, 'revision': revision,
        'variant': variant, 'token': token})


def verify_image_ticket(token: str, kb_id: str, document_id: str, revision: str,
                        asset_id: str, variant: str):
    import jwt
    from jonex_core.common import get_config
    config = get_config()
    try:
        value = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM],
            options={"require": ["exp", "tid", "uid", "kb", "doc", "rev", "asset", "variant", "scope"]})
        if (value["scope"] != "wiki_image_view_v1" or not value["uid"] or not value["tid"]
            or (value["kb"], value["doc"], value["rev"], value["asset"], value["variant"])
            != (kb_id, document_id, revision, asset_id, variant)):
            return None
        return value
    except jwt.PyJWTError:
        return None
