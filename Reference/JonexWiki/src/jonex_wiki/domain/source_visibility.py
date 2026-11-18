"""Prevent new queries from using a publication containing deleted sources."""
import json
from functools import lru_cache

from sqlalchemy import select

from jonex_wiki.kernel.errors import ConflictError
from jonex_wiki.storage.models import DocumentRow


@lru_cache(maxsize=32)
def bundle_document_ids(published):
    provenance = json.loads((published / "provenance.json").read_text("utf-8"))
    return frozenset(item["document_id"]
                     for item in provenance["source_inventory"]["entries"])


async def ensure_sources_visible(session, tenant_id, kb_id, published):
    deleted = set((await session.execute(select(DocumentRow.document_id).where(
        DocumentRow.tenant_id == tenant_id, DocumentRow.kb_id == kb_id,
        DocumentRow.deleted_at.isnot(None)))).scalars())
    if deleted and deleted.intersection(bundle_document_ids(published)):
        raise ConflictError("已发布版本包含已删除来源；请等待增量编译完成后阅读或检索")
