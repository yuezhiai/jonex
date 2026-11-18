"""Bounded literal search of registered, authored article bodies."""
import asyncio
import base64
import hashlib
import json

from jonex_wiki.domain.reading.authored_articles import _BundleEvidenceAccess, _safe_read, authored_articles
from jonex_wiki.domain.reading.image_requests import pinned_view
from jonex_wiki.kernel.errors import ValidationError

_MAX_FILES = 64
_MAX_BYTES = 8 * 1024 * 1024


def search_articles(view, pub, options):
    if set(options) - {'revision', 'query', 'document_id', 'cursor'}:
        raise ValidationError('ARTICLE_SEARCH_FIELDS_INVALID')
    query = options.get('query', '')
    document_id = options.get('document_id', '')
    cursor = options.get('cursor', '')
    if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 256
            or not isinstance(document_id, str) or len(document_id) > 128
            or not isinstance(cursor, str) or len(cursor) > 4096):
        raise ValidationError('ARTICLE_SEARCH_VALUE_INVALID')
    query = query.strip().casefold()
    scope = hashlib.sha256(json.dumps([view.tenant_id, view.context_id, view.manifest_hash,
                                      document_id, query], ensure_ascii=False).encode()).hexdigest()
    offset = 0
    if cursor:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor))
            offset = token['offset']
            if token['scope'] != scope or type(offset) is not int or offset < 0:
                raise ValueError()
        except Exception as exc:
            raise ValidationError('ARTICLE_SEARCH_CURSOR_SCOPE_MISMATCH') from exc
    articles = authored_articles(pub)
    if document_id:
        articles = [item for item in articles if document_id in item['document_ids']]
    if offset > len(articles):
        raise ValidationError('ARTICLE_SEARCH_CURSOR_INVALID')
    access = _BundleEvidenceAccess(pub)
    matches, consumed, scanned = [], 0, 0
    while offset < len(articles) and scanned < _MAX_FILES:
        article = articles[offset]
        path = access.path(article['path'])
        size = path.stat().st_size
        if scanned and consumed + size > _MAX_BYTES:
            break
        body = _safe_read(access, article['path'])
        consumed += size
        scanned += 1
        offset += 1
        if query in body.casefold():
            matches.append({'path': article['path']})
    more = offset < len(articles)
    next_cursor = base64.urlsafe_b64encode(json.dumps({'scope': scope, 'offset': offset}).encode()).decode() if more else None
    return {'revision': view.manifest_hash, 'items': matches, 'scanned': scanned,
            'has_more': more, 'cursor': next_cursor, 'search_mode': 'literal_authored_body'}


async def query_articles(layout, tenant_id, kb_id, options, *, visibility_guard=None):
    revision = options.get('revision', '')
    if not isinstance(revision, str):
        raise ValidationError('ARTICLE_SEARCH_REVISION_INVALID')
    async with layout.version_lock(tenant_id, kb_id):
        view = pinned_view(layout, tenant_id, kb_id, revision)
        pub = layout.versions_dir(tenant_id, kb_id, view.manifest_hash)
        if visibility_guard:
            await visibility_guard(pub)
        return await asyncio.to_thread(search_articles, view, pub, options)
