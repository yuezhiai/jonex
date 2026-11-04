"""Authorized caller supplies the scope; stream pinned pictures without base64."""

import asyncio
import hashlib
import re
from urllib.parse import quote

from starlette.responses import Response, StreamingResponse

from jonex_wiki.kernel.errors import ValidationError

from .image_requests import pinned_view
from .image_service import get_image, image_file


async def image_response(
    layout, tenant_id, kb_id, revision, asset_id, document_id, variant, headers
):
    if not revision:
        raise ValidationError("IMAGE_REVISION_REQUIRED")
    async with layout.version_lock(tenant_id, kb_id):
        view = pinned_view(layout, tenant_id, kb_id, revision)
        asset = await asyncio.to_thread(get_image, view, asset_id, document_id)
        path, rendition = await asyncio.to_thread(
            image_file,
            view,
            asset,
            variant=variant,
            cache_dir=layout.kb_dir(tenant_id, kb_id) / "visual/image-renditions",
        )
        stream = path.open("rb")
        try:
            # Original bytes are already verified by BundleView. Check generated
            # bytes on open, so a damaged cache cannot become visual evidence.
            if rendition["variant"] != "original":
                digest = await asyncio.to_thread(hashlib.file_digest, stream, "sha256")
                if digest.hexdigest() != rendition["sha256"]:
                    raise ValidationError("IMAGE_RENDITION_HASH_MISMATCH")
            size = path.stat().st_size
            out = {
                "ETag": '"' + rendition["sha256"] + '"',
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, max-age=120",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "inline; filename*=UTF-8''" + quote(path.name),
            }
            if headers.get("if-none-match") == out["ETag"]:
                stream.close()
                return Response(status_code=304, headers=out)
            start, end, status = 0, size - 1, 200
            requested = headers.get("range")
            if requested and headers.get("if-range", out["ETag"]) == out["ETag"]:
                match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", requested)
                if not match or not any(match.groups()):
                    stream.close()
                    return Response(
                        status_code=416,
                        headers={**out, "Content-Range": f"bytes */{size}"},
                    )
                if match[1]:
                    start = int(match[1])
                    end = min(int(match[2]), size - 1) if match[2] else size - 1
                else:
                    start = max(0, size - int(match[2]))
                if start >= size or end < start:
                    stream.close()
                    return Response(
                        status_code=416,
                        headers={**out, "Content-Range": f"bytes */{size}"},
                    )
                status = 206
                out["Content-Range"] = f"bytes {start}-{end}/{size}"
            out["Content-Length"] = str(end - start + 1)
            stream.seek(start)
        except BaseException:
            stream.close()
            raise

    def chunks():
        remaining = end - start + 1
        try:
            while remaining:
                data = stream.read(min(256 * 1024, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data
        finally:
            stream.close()

    return StreamingResponse(
        chunks(), status_code=status, media_type=rendition["mime_type"], headers=out
    )
