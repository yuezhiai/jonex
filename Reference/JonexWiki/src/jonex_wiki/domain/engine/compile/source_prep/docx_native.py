"""Supplement delivery with exact OOXML values, without inventing physical page mappings."""
from __future__ import annotations

import hashlib
import html
import json
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _text(node: ET.Element) -> str:
    # Deleted runs are audit material, not current text. Inserts remain visible.
    def walk(element):
        if element.tag in {W + "del", W + "moveFrom", W + "tbl"}:
            return ""
        if element.tag == W + "t":
            return element.text or ""
        if element.tag == W + "tab":
            return "\t"
        if element.tag in {W + "br", W + "cr"}:
            return "\n"
        return "".join(walk(child) for child in element)
    return walk(node)


def extract_docx(path: Path, output_dir: Path | None = None) -> tuple[list[dict], dict]:
    with zipfile.ZipFile(path) as archive:
        # Reject expanding archives before parsing; macros and embedded objects never execute.
        if sum(i.file_size for i in archive.infolist()) > 256 * 1024 * 1024:
            raise ValueError("DOCX_UNCOMPRESSED_SIZE_LIMIT")
        document = ET.fromstring(archive.read("word/document.xml"))
        relationships = {}
        if "word/_rels/document.xml.rels" in archive.namelist():
            for rel in ET.fromstring(archive.read("word/_rels/document.xml.rels")):
                relationships[rel.get("Id")] = {"target": rel.get("Target"), "mode": rel.get("TargetMode"), "type": rel.get("Type")}
        body = document.find(W + "body")
        if body is None:
            raise ValueError("DOCX_BODY_MISSING")
        items, tables, warnings, element_locators = [], [], [], {}
        ordinal = 0

        def table(node, parent=None, before="", after="", body_index=None):
            nonlocal ordinal
            ordinal += 1
            index = ordinal
            locator = {"kind": "docx_table", "table_ordinal": index, "parent": parent}
            if body_index is not None:
                locator['body_index'] = body_index
            insert_at = len(items)
            element_locators[id(node)] = locator
            cells, active, rows = [], {}, []
            for ri, tr in enumerate(node.findall(W + "tr"), 1):
                row, ci, next_active = [], 1, {}
                before_el = tr.find(W + "trPr/" + W + "gridBefore")
                if before_el is not None:
                    ci += int(before_el.get(W + "val", "0"))
                    warnings.append({"table": index, "row": ri, "kind": "omitted_grid_columns", "count": ci - 1})
                    row += [{"row": ri, "col": c, "rowspan": 1, "colspan": 1, "text": ""} for c in range(1, ci)]
                for tc in tr.findall(W + "tc"):
                    element_locators[id(tc)] = {**locator, "row": ri, "column": ci}
                    span = tc.find(W + "tcPr/" + W + "gridSpan")
                    width = int(span.get(W + "val", "1")) if span is not None else 1
                    merge = tc.find(W + "tcPr/" + W + "vMerge")
                    continuation = merge is not None and merge.get(W + "val", "continue") != "restart"
                    value = "\n".join(_text(p) for p in tc.findall(W + "p"))
                    if continuation:
                        anchor = active.get(ci)
                        if anchor is None or anchor["colspan"] != width:
                            raise ValueError(f"DOCX_INVALID_VERTICAL_MERGE: table={index}, row={ri}, col={ci}")
                        anchor["rowspan"] += 1
                        next_active[ci] = anchor
                    else:
                        cell = {"row": ri, "col": ci, "rowspan": 1, "colspan": width,
                                "text": value, "locator": {**locator, "row": ri, "column": ci}}
                        row.append(cell)
                        cells.append(cell)
                        if merge is not None:
                            next_active[ci] = cell
                    for nested in tc.findall(W + "tbl"):
                        table(nested, {**locator, "row": ri, "column": ci}, body_index=body_index)
                    ci += width
                active = next_active
                rows.append(row)
            markup = "<table>" + "".join("<tr>" + "".join(
                f'<td rowspan="{c["rowspan"]}" colspan="{c["colspan"]}">{html.escape(c["text"]).replace(chr(10), "<br/>")}</td>'
                for c in row) + "</tr>" for row in rows) + "</table>"
            captions = [v for v in (before, after) if v.strip()]
            tables.append({"ordinal": index, "parent": parent, "cells": cells,
                           "row_count": len(rows), "column_count": max((c["col"] + c["colspan"] - 1 for c in cells), default=0)})
            items.insert(insert_at, {"type": "table", "table_body": markup,
                "table_caption": [f"原文件第 {index} 张表", *captions],
                "native_locator": locator, "evidence_kind": "native_structure",
                "processor": "wiki.docx.ooxml.v1", "parent_object": parent,
                "quality": {"structure": "native", "values": "exact_ooxml_text", "page_mapping": "unavailable"}})

        children = list(body)
        for pos, node in enumerate(children):
            if node.tag == W + "p":
                element_locators[id(node)] = {"kind": "docx_body", "body_index": pos}
                value = _text(node)
                if value.strip():
                    items.append({"type": "text", "text": value, "native_locator": {"kind": "docx_body", "body_index": pos}, "evidence_kind": "native_text"})
            elif node.tag == W + "tbl":
                previous = _text(children[pos - 1]) if pos and children[pos - 1].tag == W + "p" else ""
                following = _text(children[pos + 1]) if pos + 1 < len(children) and children[pos + 1].tag == W + "p" else ""
                table(node, before=previous, after=following, body_index=pos)
        parents = {id(child): parent for parent in document.iter() for child in parent}
        body_positions = {id(child): pos for pos, child in enumerate(children)}
        media = []
        for number, blip in enumerate(document.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip"), 1):
            rel_id = blip.get(R + "embed") or blip.get(R + "link")
            relation = relationships.get(rel_id) or {}
            node = blip;parent_locator = None
            while id(node) in parents:
                node = parents[id(node)]
                if id(node) in element_locators:
                    parent_locator = element_locators[id(node)];break
            if relation.get("mode") == "External":
                warnings.append({"kind": "external_image_not_fetched", "relationship": rel_id});continue
            member = posixpath.normpath("word/" + str(relation.get("target") or ""))
            if member not in archive.namelist() or not member.startswith("word/"):
                warnings.append({"kind": "native_image_missing", "relationship": rel_id});continue
            data = archive.read(member);digest = hashlib.sha256(data).hexdigest()
            relative = "images/docx-" + digest + Path(member).suffix.lower()
            media.append({"ordinal": number, "path": relative, "sha256": digest,
                          "parent": parent_locator, "relationship": rel_id})
            if output_dir is not None:
                target = output_dir / relative;target.parent.mkdir(parents=True, exist_ok=True);target.write_bytes(data)
                ancestor = blip
                picture = None
                body_index = None
                while id(ancestor) in parents:
                    ancestor = parents[id(ancestor)]
                    if ancestor.tag.endswith('}pic') and picture is None:
                        picture = ancestor
                    if id(ancestor) in body_positions:
                        body_index = body_positions[id(ancestor)]
                        break
                transform = {}
                if picture is not None:
                    drawing_ns = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
                    crop = picture.find('.//' + drawing_ns + 'srcRect')
                    if crop is not None:
                        transform['crop'] = {key: int(crop.get(key, '0')) / 100000 for key in ('l', 't', 'r', 'b')}
                    xfrm = picture.find('.//' + drawing_ns + 'xfrm')
                    if xfrm is not None:
                        transform.update(rotation=int(xfrm.get('rot', '0')) / 60000,
                                         flip_h=xfrm.get('flipH') in {'1', 'true'}, flip_v=xfrm.get('flipV') in {'1', 'true'})
                source_caption = ''
                caption_status = 'unresolved'
                if body_index is not None and body_index + 1 < len(children):
                    candidate = children[body_index + 1]
                    style = candidate.find(W + 'pPr/' + W + 'pStyle')
                    label = _text(candidate).strip() if candidate.tag == W + 'p' else ''
                    if style is not None and style.get(W + 'val', '').casefold() in {'caption', '题注', '图题'}:
                        source_caption, caption_status = label, 'native_caption_style'
                items.append({"type": "image", "img_path": relative,
                    "image_caption": source_caption,
                    "source_caption": source_caption, "caption_status": caption_status,
                    "native_locator": {"kind": "docx_image", "relationship_id": rel_id,
                        "parent": parent_locator, "body_index": body_index, "occurrence": number,
                        "transform": transform},
                    "parent_object": parent_locator, "evidence_kind": "native_image_unread",
                    "processor": "wiki.docx.ooxml.v1"})
        if "word/footnotes.xml" in archive.namelist():
            footnotes = ET.fromstring(archive.read("word/footnotes.xml"))
            definitions = {n.get(W + "id"): "\n".join(_text(p) for p in n.findall(W + "p")) for n in footnotes.findall(W + "footnote")}
            for reference in document.findall(".//" + W + "footnoteReference"):
                key = reference.get(W + "id")
                if definitions.get(key):
                    items.append({"type": "text", "text": f"脚注 {key}：{definitions[key]}",
                        "native_locator": {"kind": "docx_footnote", "footnote_id": key}, "evidence_kind": "native_text"})
        # Inventory complex objects even when the consumer cannot render them yet.
        counts = {name: len(document.findall(".//" + W + tag)) for name, tag in (
            ("tables", "tbl"), ("drawings", "drawing"), ("textboxes", "txbxContent"),
            ("deleted_runs", "del"), ("inserted_runs", "ins"), ("embedded_objects", "object"), ("footnote_references", "footnoteReference"))}
        report = {"schema": "wiki.docx.inventory.v1", "objects": counts, "tables": tables,
            "media": media,
            "relationships": relationships, "warnings": warnings,
            "gaps": [k for k in ("drawings", "textboxes", "embedded_objects", "footnote_references") if counts[k]],
            "original_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    # Images are extracted through relationships, but remain beside their
    # owning paragraph rather than all moving to the document's end.
    def source_order(pair):
        index, item = pair
        locator = item.get('native_locator') or {}
        parent = locator.get('parent') or {}
        position = locator.get('body_index', parent.get('body_index'))
        return (position if isinstance(position, int) else 10**9, item.get('type') == 'image', index)
    items = [item for _, item in sorted(enumerate(items), key=source_order)]
    return items, report


def supplement_docx(items: list[dict], original: Path, output_dir: Path) -> tuple[list[dict], dict]:
    native, inventory = extract_docx(original, output_dir)
    native_tables = [i for i in native if i["type"] == "table"]
    upstream_tables = [i for i in items if i.get("type") in {"table", "complex_table"}]
    # Alignment is a candidate, never a fabricated page locator. Keep all original observations.
    for index, item in enumerate(upstream_tables):
        value = str(item.get("table_body") or item.get("html") or item.get("text") or "")
        tokens = {html.unescape(t).strip() for t in re.split(r"<[^>]+>", value) if len(html.unescape(t).strip()) > 1}
        scores = []
        for candidate in native_tables:
            text = html.unescape(re.sub(r"<[^>]+>", " ", candidate["table_body"]))
            score = sum(t in text for t in tokens) / len(tokens) if tokens else 0
            if score >= .7:
                scores.append(candidate["native_locator"])
        inventory.setdefault("alignment", []).append({"upstream_table": index,
            "native_candidates": scores, "status": "unique" if len(scores) == 1 else "unresolved"})
    inventory["upstream_table_count"] = len(upstream_tables)
    inventory["upstream_observations_retained"] = "docx-upstream-tables.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "docx-upstream-tables.json").write_text(json.dumps(upstream_tables, ensure_ascii=False), "utf-8")
    (output_dir / "docx-inventory.json").write_text(json.dumps(inventory, ensure_ascii=False), "utf-8")
    # OOXML owns native table values; upstream still supplies visual and non-native modalities.
    # Native paragraphs prevent OCR table fragments from re-entering as contradictory text facts.
    visual = [i for i in items if i.get("type") in {"image", "chart", "equation", "interline_equation", "audio", "video"}]
    # Exact bytes establish identity, not reading-order proximity. Preserve
    # upstream observations on the native occurrence instead of duplicating it.
    native_by_sha = {}
    for candidate in native:
        if candidate['type'] == 'image':
            relative = candidate.get('img_path') or ''
            path = (output_dir / relative).resolve()
            if path.is_relative_to(output_dir.resolve()) and path.is_file():
                with path.open('rb') as stream:
                    native_by_sha.setdefault(hashlib.file_digest(stream, 'sha256').hexdigest(), []).append(candidate)
    retained_visual = []
    for observation in visual:
        if observation.get('type') not in {'image', 'chart'}:
            retained_visual.append(observation)
            continue
        relative = observation.get('img_path') or observation.get('image_path') or observation.get('chart_path') or ''
        path = (output_dir / relative).resolve()
        matched = []
        if relative and path.is_relative_to(output_dir.resolve()) and path.is_file():
            with path.open('rb') as stream:
                matched = native_by_sha.get(hashlib.file_digest(stream, 'sha256').hexdigest(), [])
        if matched:
            for candidate in matched:
                candidate.setdefault('upstream_observations', []).append(observation)
                candidate['ocr_text'] = observation.get('ocr_text') or observation.get('text_content') or ''
                candidate['description'] = observation.get('description') or observation.get('image_caption') or ''
        else:
            retained_visual.append(observation)
    return [*native, *retained_visual], inventory
