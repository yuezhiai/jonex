"""Attach verified source illustrations and actual image observations.

Names, titles, graph edges, text proximity and question wording do not prove an
image association. Optional scenarios own their semantic image relationships;
ordinary text citations use only exact source markers/captions. Historical asset IDs
remain readable through the version-scoped media API.
"""
from __future__ import annotations

import hashlib
import json

CATALOG = "query/media_catalog.json"
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def entity_frontier(view, question):
    """Retired artwork-only shortcut; generic graph discovery owns navigation."""
    return None


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
                and (a.get('source_marker') in excerpt or a.get('caption') and a['caption'] in excerpt)
                and a['association_method'] in {'source_marker', 'source_caption'}]
            if not candidates and len(excerpt) >= 4:
                hits = find_images(view, {'query': excerpt[:512], 'document_id': citation['document_id'], 'limit': 3})
                candidates = [a for a in hits['items'] if len(a.get('caption', '')) >= 4 and a['caption'] in excerpt]
        for asset in candidates:
            if asset['asset_id'] in used or len(used) >= 3:
                continue
            used.add(asset['asset_id'])
            citation.setdefault('media_assets', []).append({**asset, 'bundle_sha': view.manifest_hash})
    return citations
