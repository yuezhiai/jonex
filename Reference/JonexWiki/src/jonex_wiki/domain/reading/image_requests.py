"""Picture reads use only the sealed picture index, without resource/graph scans."""

import asyncio
import re

from jonex_wiki.adapters.bundle_view import DirectoryBundleView
from jonex_wiki.kernel.errors import NotFoundError, ValidationError

from .image_service import find_images, get_image


def pinned_view(layout, tenant_id, kb_id, revision=""):
    if revision and not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise ValidationError("IMAGE_REVISION_INVALID")
    pub = (
        layout.versions_dir(tenant_id, kb_id, revision)
        if revision
        else layout.resolve_published(tenant_id, kb_id)
    )
    versions = (layout.kb_dir(tenant_id, kb_id) / "compile/versions").resolve()
    if not pub or not pub.is_dir() or pub.resolve().parent != versions:
        raise NotFoundError("IMAGE_PUBLICATION_NOT_FOUND")
    view = DirectoryBundleView(tenant_id=tenant_id, context_id=kb_id, revision_dir=pub)
    if (
        view.tenant_id != tenant_id
        or view.context_id != kb_id
        or revision
        and view.manifest_hash != revision
    ):
        raise ValidationError("IMAGE_SCOPE_MISMATCH")
    return view


async def query_images(layout, tenant_id, kb_id, operation, options):
    revision = options.get("revision", "")
    if not isinstance(revision, str):
        raise ValidationError("IMAGE_REVISION_INVALID")
    if operation != "find_images" and not revision:
        raise ValidationError("IMAGE_REVISION_REQUIRED")
    async with layout.version_lock(tenant_id, kb_id):
        view = pinned_view(layout, tenant_id, kb_id, revision)
        if operation == "find_images":
            return await asyncio.to_thread(find_images, view, options)
        if operation != "get_image" or set(options) - {
            "revision",
            "asset_id",
            "document_id",
        }:
            raise ValidationError("IMAGE_OPERATION_FIELDS_INVALID")
        asset = await asyncio.to_thread(
            get_image, view, options.get("asset_id"), options.get("document_id", "")
        )
        return {"revision": view.manifest_hash, "image": asset}
