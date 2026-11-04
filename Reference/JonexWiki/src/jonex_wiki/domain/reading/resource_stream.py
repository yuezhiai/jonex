"""Pinned media bytes with HTTP ranges; source files are never embedded in JSON."""
import hashlib
import asyncio
import json
import mimetypes
import re
import os
from collections import OrderedDict
from threading import RLock
from urllib.parse import quote
from starlette.responses import Response, StreamingResponse
from jonex_wiki.adapters.bundle_view import DirectoryBundleView
from jonex_wiki.kernel.errors import NotFoundError, ValidationError

_PROOFS = OrderedDict()
_LOCK = RLock()


async def media_response(layout, tenant_id, kb_id, revision, object_id, headers):
    if not re.fullmatch(r"[0-9a-f]{64}", revision or ""):
        raise ValidationError("RESOURCE_REVISION_INVALID")
    async with layout.version_lock(tenant_id, kb_id):
        pub = layout.versions_dir(tenant_id, kb_id, revision)
        if not pub.is_dir():raise NotFoundError("RESOURCE_PUBLICATION_NOT_FOUND")
        view = DirectoryBundleView(tenant_id=tenant_id, context_id=kb_id, revision_dir=pub)
        resources = json.loads(view.read_bytes("query/resources.json"))
        obj = next((o for o in resources["objects"] if o["object_id"] == object_id), None)
        if obj is None:raise NotFoundError("RESOURCE_OBJECT_NOT_FOUND")
        if obj["kind"] == "video_frame":
            base = pub.resolve();target = (base / obj["asset_path"]).resolve()
            expected = obj["asset_sha256"];filename = target.name
        elif obj["kind"] == "media_segment":
            source = next(o for o in resources["objects"] if o["kind"] == "document" and o["document_id"] == obj["document_id"])
            original = source.get("original") or {}
            base = layout.raw_dir(tenant_id, kb_id, obj["document_id"], source["content_generation"]).resolve()
            target = (base / (original.get("path") or "missing")).resolve()
            expected = source["source_sha256"];filename = source["label"]
        else:raise ValidationError("RESOURCE_NOT_STREAMABLE_MEDIA")
        if not target.is_relative_to(base) or not target.is_file():raise NotFoundError("RESOURCE_MEDIA_UNAVAILABLE")
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if not media_type.startswith(("video/", "audio/")) and media_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValidationError("RESOURCE_MEDIA_TYPE_UNSUPPORTED")
        stream = target.open("rb")  # Pin before releasing the publication lock.
        try:
            stat = os.fstat(stream.fileno())
            proof = (str(target), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, expected)
            with _LOCK:known = proof in _PROOFS
            if not known:
                digest = (await asyncio.to_thread(hashlib.file_digest, stream, "sha256")).hexdigest()
                if digest != expected:raise ValidationError("RESOURCE_MEDIA_HASH_MISMATCH")
                with _LOCK:
                    _PROOFS[proof] = True
                    while len(_PROOFS) > 128:_PROOFS.popitem(last=False)
            size = stat.st_size
            response_headers = {"Accept-Ranges": "bytes", "ETag": '"' + expected + '"',
                "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
                "Content-Disposition": "inline; filename*=UTF-8''" + quote(filename)}
            if headers.get("if-none-match") == response_headers["ETag"]:
                stream.close();return Response(status_code=304, headers=response_headers)
            start, end, status = 0, size - 1, 200
            range_header = headers.get("range")
            if range_header and headers.get("if-range", response_headers["ETag"]) == response_headers["ETag"]:
                match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", range_header)
                if not match or not any(match.groups()):
                    stream.close();return Response(status_code=416, headers={**response_headers, "Content-Range": f"bytes */{size}"})
                if match[1]:start = int(match[1]);end = min(int(match[2]), size - 1) if match[2] else size - 1
                else:start = max(0, size - int(match[2]))
                if start >= size or end < start:
                    stream.close();return Response(status_code=416, headers={**response_headers, "Content-Range": f"bytes */{size}"})
                status = 206;response_headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            response_headers["Content-Length"] = str(max(0, end - start + 1));stream.seek(start)
        except BaseException:
            stream.close();raise

    def chunks():
        remaining = max(0, end - start + 1)
        try:
            while remaining:
                data = stream.read(min(256 * 1024, remaining))
                if not data:break
                remaining -= len(data);yield data
        finally:stream.close()
    return StreamingResponse(chunks(),status_code=status,media_type=media_type,headers=response_headers)
