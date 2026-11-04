"""Resolve a pinned publication and execute bounded graph reads."""
import asyncio
import re

from jonex_wiki.domain.reading.knowledge_graph import INDEX_PATH, SCHEMA_VERSION, GraphIndex, build_index
from jonex_wiki.kernel.errors import NotFoundError, ValidationError

QUERY_FIELDS = {
    "media": ("graph_revision", "document_id", "asset_id", "include_content"),
    "types": ("graph_revision", "document_id"),
    "search": ("graph_revision", "document_id", "query", "node_type", "limit", "cursor"),
    "node": ("graph_revision", "document_id", "node_id", "include_content"),
    "neighbors": ("graph_revision", "document_id", "node_id", "direction", "families", "limit", "cursor"),
    "evidence": ("graph_revision", "document_id", "node_id", "edge_id", "evidence_id", "limit", "cursor"),
}

async def query_graph(layout, tenant_id: str, kb_id: str, operation: str, options: dict) -> dict:
    options = dict(options)
    revision = str(options.pop("graph_revision", "") or "")
    include_content = options.pop("include_content", False)
    if operation not in QUERY_FIELDS:
        raise ValidationError("未知图查询操作")
    if operation not in ("types", "search") and not revision:
        raise ValidationError("后续图查询必须携带 graph_revision；请先搜索节点")
    if revision and not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise ValidationError("graph_revision 必须是发布版本的 64 位哈希")
    # Retention cannot remove this publication while the request is reading it.
    async with layout.version_lock(tenant_id, kb_id):
        versions = (layout.kb_dir(tenant_id, kb_id) / "compile" / "versions").resolve()
        pub = layout.versions_dir(tenant_id, kb_id, revision) if revision else layout.resolve_published(tenant_id, kb_id)
        if pub is None or not pub.is_dir() or pub.resolve().parent != versions:
            raise NotFoundError("图发布版本不存在或已过期，请重新搜索")
        pub = pub.resolve()
        revision = pub.name
        if operation == "media":
            from jonex_wiki.adapters.bundle_view import DirectoryBundleView
            from jonex_wiki.domain.reading.media_service import read_media
            view = DirectoryBundleView(tenant_id=tenant_id, context_id=kb_id, revision_dir=pub)
            if (view.manifest_hash != revision or view.tenant_id != tenant_id or view.context_id != kb_id):
                raise ValidationError("图片发布版本身份不匹配")
            return await asyncio.to_thread(read_media, view, options.get("asset_id"), options.get("document_id"), include_content)
        index_path = pub / INDEX_PATH
        if index_path.is_file() and (pub / "indexes/manifest.json").is_file():
            from jonex_wiki.adapters.bundle_view import DirectoryBundleView
            view = DirectoryBundleView(tenant_id=tenant_id, context_id=kb_id, revision_dir=pub)
            if view.tenant_id != tenant_id or view.context_id != kb_id or view.manifest_hash != revision:
                raise ValidationError("图发布版本身份不匹配")
            expected = next((entry["sha256"] for entry in view.manifest["files"] if entry["path"] == INDEX_PATH), None)
            if not expected:
                raise ValidationError("图索引不在发布清单中")
            if not view.has_verified_file(INDEX_PATH, expected, index_path.stat()):
                await asyncio.to_thread(view.read_bytes, INDEX_PATH)
        if not index_path.is_file():
            # Older immutable bundles are never edited. Each version has its own
            # rebuildable cache, with schema changes occupying a new directory.
            index_path = layout.kb_dir(tenant_id, kb_id) / "compile" / "graph-cache" / SCHEMA_VERSION / (revision + ".sqlite")
            if not index_path.is_file():
                await asyncio.to_thread(build_index, pub, index_path, tenant_id, kb_id)
        index = GraphIndex(index_path, revision, tenant_id, kb_id)
        try:
            result = await asyncio.to_thread(getattr(index, operation), **options)
            if operation == "node" and include_content and result["node"].get("page_path"):
                from jonex_wiki.domain.reading.bundle_reader import _strip_frontmatter
                target = (pub / "wiki" / result["node"]["page_path"]).resolve()
                if not target.is_relative_to((pub / "wiki").resolve()) or not target.is_file():
                    raise NotFoundError("该版本的 Wiki 页面不存在")
                if (pub / "indexes/manifest.json").is_file():
                    from jonex_wiki.adapters.bundle_view import DirectoryBundleView
                    page_view = DirectoryBundleView(tenant_id=tenant_id, context_id=kb_id, revision_dir=pub)
                    body = (await asyncio.to_thread(page_view.read_bytes, "wiki/" + result["node"]["page_path"])).decode("utf-8")
                else:
                    body = await asyncio.to_thread(target.read_text, "utf-8")
                result["node"]["content"] = _strip_frontmatter(body)
            return result
        except TypeError as exc:
            raise ValidationError("图查询参数无效") from exc
