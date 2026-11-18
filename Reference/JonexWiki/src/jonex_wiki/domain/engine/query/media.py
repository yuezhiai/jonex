"""Attach verified source illustrations and actual image observations.

Names, titles, graph edges, text proximity and question wording do not prove an
image association. Optional scenarios own their semantic image relationships;
ordinary text citations use only exact source markers/captions. Historical asset IDs
remain readable through the version-scoped media API.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import re

CATALOG = "query/media_catalog.json"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_ANSWER_IMAGE_PATH = re.compile(
    r"(?<![A-Za-z0-9_./-])(media/assets/([0-9a-f]{64})\.[a-z0-9]{1,8})(?![A-Za-z0-9_./-])"
)


def _nonempty_exact_match(value, excerpt):
    return isinstance(value, str) and bool(value.strip()) and value in excerpt


def entity_frontier(view, question):
    """Retired artwork-only shortcut; generic graph discovery owns navigation."""
    return None


def _selected_answer_images(citations, view, answer, used):
    """Return only unambiguous, registered images explicitly named in the answer.

    A selected file is an illustration, never proof that the model read its pixels.
    Its occurrence must be unique among documents with verified citations.
    """
    from jonex_wiki.domain.reading.image_service import _validate_index, open_index

    paths = list(dict.fromkeys((m.group(1), m.group(2))
                               for m in _ANSWER_IMAGE_PATH.finditer(answer)))[:3]
    documents = list(dict.fromkeys(c.get('document_id') for c in citations
                                   if isinstance(c.get('document_id'), str) and c['document_id']))
    if not paths or not documents:
        return
    with closing(open_index(view)) as db:
        _validate_index(db)
        for path, digest in paths:
            matches = []
            for doc_id in documents:
                rows = db.execute(
                    "SELECT payload FROM images WHERE document_id=? "
                    "AND json_extract(payload, '$.path')=? LIMIT 2", (doc_id, path),
                ).fetchall()
                matches.extend(json.loads(row[0]) for row in rows)
                if len(matches) > 1:
                    break
            if len(matches) != 1:
                continue
            asset = matches[0]
            if (asset.get('path') != path or asset.get('sha256') != digest
                    or asset.get('asset_id') in used or len(used) >= 3):
                continue
            # Confirm this file belongs to the sealed publication before exposing it.
            view.verified_file_path(path)
            eligible = [c for c in citations if c.get('document_id') == asset['document_id']]
            preferred = next((c for c in eligible if any(
                _nonempty_exact_match(asset.get(key), str(c.get('excerpt') or ''))
                for key in ('caption', 'ocr_text'))), eligible[0])
            preferred.setdefault('media_assets', []).append({**asset,
                'bundle_sha': view.manifest_hash, 'usage': 'illustration',
                'association_method': 'answer_selected_registered_path',
                'actual_image_delivered': False})
            used.add(asset['asset_id'])


def present_selected_images(answer, citations):
    """Replace a validated internal file path with its public evidence handle."""
    for citation in citations:
        handle = citation.get('handle')
        if not isinstance(handle, str) or not handle:
            continue
        for asset in citation.get('media_assets') or []:
            if asset.get('association_method') == 'answer_selected_registered_path':
                path = asset['path']
                answer = answer.replace('`' + path + '`', handle).replace(path, handle)
    return answer


def _attach_legacy_media(citations, view, question, answer):
    """Require an exact image_asset evidence identity in a final citation.

    This does not create visual evidence or infer any object/author identity.
    The reader producing that evidence must have actually delivered the image.
    """
    clean = [{k: v for k, v in citation.items() if k != "media_assets"}
             for citation in citations]
    if CATALOG not in view.list_entries():
        return clean
    catalog = json.loads(view.read_bytes(CATALOG))
    assets = {asset["asset_id"]: asset for asset in catalog.get("assets", [])}
    for citation in clean:
        evidence = citation.get("evidence_ref") or {}
        if (evidence.get("kind") != "image_asset"
                or evidence.get("revision") != view.manifest_hash):
            continue
        asset = assets.get(evidence.get("asset_id"))
        if (not asset or asset.get("document_id") != citation.get("document_id")
                or evidence.get("document_id") != asset.get("document_id")
                or not asset.get("sha256")
                or evidence.get("sha256") != asset["sha256"]):
            continue
        data = view.read_bytes(asset["path"])
        if len(data) > MAX_IMAGE_BYTES or hashlib.sha256(data).hexdigest() != asset["sha256"]:
            continue
        citation["media_assets"] = [{**asset, "bundle_sha": view.manifest_hash,
            "association_method": "direct_image_evidence"}]
    return clean


def attach_media(citations, view, question, answer):
    """Attach only exact source associations, independently of author or domain.

    Ordinary attachments are illustrations. Only image_observation references
    created by an actual read carry visual_evidence and a read receipt.
    """
    from jonex_wiki.domain.reading.image_service import get_image, page_images, find_images
    if 'indexes/images.sqlite' not in view.list_entries():
        return _attach_legacy_media(citations, view, question, answer)
    used = set()
    # A metadata citation and a visual observation may cite the same picture.
    # Reserve the single attachment for the actual read, regardless of order.
    ordered = sorted(citations, key=lambda c: 0 if
        (c.get('evidence_ref') or {}).get('kind') == 'image_observation' and
        (c.get('evidence_ref') or {}).get('read_id') else 1)
    for citation in ordered:
        evidence = citation.get('evidence_ref') or {}
        candidates = []
        if evidence.get('kind') == 'image_observation' and evidence.get('read_id'):
            candidates = [{**get_image(view, evidence['asset_id'], citation['document_id']),
                'usage': 'visual_evidence', 'visual_read_id': evidence['read_id'],
                'association_method': 'actual_image_read', 'actual_image_delivered': True}]
        elif evidence.get('kind') == 'image_metadata' and evidence.get('asset_id'):
            candidates = [{**get_image(view, evidence['asset_id'], citation['document_id']),
                'usage': 'illustration', 'association_method': 'selected_source_image',
                'actual_image_delivered': False}]
        elif 'indexes/images.sqlite' in view.list_entries():
            # Only exact captions/markers inside a verified delivered excerpt.
            # A source page gallery or merely being on the same PDF page is not enough.
            excerpt = str(citation.get('excerpt') or '')
            page = citation.get('page_path') or ''
            _, matches, _ = page_images(view, [page], '', limit=6)
            candidates = [a for a in matches if a['document_id'] == citation.get('document_id')
                and (_nonempty_exact_match(a.get('source_marker'), excerpt)
                     or _nonempty_exact_match(a.get('caption'), excerpt))
                and a['association_method'] in {'source_marker', 'source_caption'}]
            if not candidates and len(excerpt) >= 4:
                hits = find_images(view, {'query': excerpt[:512], 'document_id': citation['document_id'], 'limit': 3})
                candidates = [a for a in hits['items'] if isinstance(a.get('caption'), str)
                    and len(a['caption']) >= 4 and _nonempty_exact_match(a['caption'], excerpt)]
        for asset in candidates:
            if asset['asset_id'] in used or len(used) >= 3:
                continue
            used.add(asset['asset_id'])
            citation.setdefault('media_assets', []).append({**asset, 'bundle_sha': view.manifest_hash})
    _selected_answer_images(citations, view, str(answer or ''), used)
    return citations
