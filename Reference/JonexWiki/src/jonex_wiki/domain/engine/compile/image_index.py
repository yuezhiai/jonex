"""Immutable, source-only image search. No graphs, embeddings or model calls."""

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from jonex_wiki.domain.engine.json_artifacts import load_json_artifact

INDEX = "indexes/images.sqlite"
SCHEMA = "wiki.images.v1"


def search_tokens(value):
    out = []
    for token in re.findall(
        r"[\u3400-\u9fff]+|[^\W_]+", str(value).casefold(), re.UNICODE
    ):
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            out.extend(token[i : i + 2] for i in range(max(1, len(token) - 1)))
        else:
            out.append(token)
    return list(dict.fromkeys(out))


def _documents_by_page(pub):
    docs = {}
    path = pub / "claims.jsonl"
    if path.is_file():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                claim = json.loads(line)
                for ref in [
                    *(claim.get("evidence") or []),
                    *(claim.get("source_spans") or []),
                ]:
                    if isinstance(ref, dict) and ref.get("document_id"):
                        docs.setdefault(claim.get("page_path"), set()).add(
                            ref["document_id"]
                        )
    return docs


def build_image_index(pub, catalog):
    target = Path(pub) / INDEX
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    nav_file = Path(pub) / "query/navigation.json"
    nav = (
        load_json_artifact(
            lambda rel: (Path(pub) / rel).read_bytes(), "query/navigation.json"
        )
        if nav_file.is_file()
        else {}
    )
    docs = _documents_by_page(Path(pub))
    by_doc = {}
    for asset in catalog["assets"]:
        by_doc.setdefault(asset["document_id"], []).append(asset)
    with sqlite3.connect(target) as db:
        db.executescript("""
          PRAGMA journal_mode=OFF;
          CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE images(id INTEGER PRIMARY KEY,asset_id TEXT UNIQUE,document_id TEXT,payload TEXT);
          CREATE INDEX images_doc ON images(document_id);
          CREATE VIRTUAL TABLE image_text USING fts5(label,caption,ocr,context,description,tokenize='unicode61');
          CREATE TABLE page_images(page TEXT,asset_id TEXT,anchor TEXT,method TEXT,PRIMARY KEY(page,asset_id));
          CREATE INDEX page_asset ON page_images(asset_id);
        """)
        db.execute("INSERT INTO metadata VALUES(?,?)", ("schema", SCHEMA))
        db.execute(
            "INSERT INTO metadata VALUES(?,?)",
            (
                "unresolved",
                json.dumps(catalog.get("unresolved", []), ensure_ascii=False),
            ),
        )
        for number, asset in enumerate(catalog["assets"], 1):
            db.execute(
                "INSERT INTO images VALUES(?,?,?,?)",
                (
                    number,
                    asset["asset_id"],
                    asset["document_id"],
                    json.dumps(asset, ensure_ascii=False),
                ),
            )
            fields = [
                asset.get("label", ""),
                asset.get("caption", ""),
                asset.get("ocr_text", ""),
                asset.get("context", ""),
                asset.get("description", ""),
            ]
            db.execute(
                "INSERT INTO image_text(rowid,label,caption,ocr,context,description) VALUES(?,?,?,?,?,?)",
                (number, *(" ".join(search_tokens(text)) for text in fields)),
            )
        for page, entry in (nav.get("page_index") or {}).items():
            scope = entry.get("scope") or {}
            source_doc = (
                scope.get("document_id") if page.startswith("sources/") else None
            )
            page_docs = {source_doc} if source_doc else docs.get(page, set())
            candidates = [a for doc in page_docs for a in by_doc.get(doc, [])]
            wiki = Path(pub) / "wiki" / page
            if not candidates or not wiki.is_file():
                continue
            body = wiki.read_text("utf-8")
            if body.startswith("---\n") and "\n---" in body[4:]:
                body = body.split("\n---", 1)[1]
            caption_counts = Counter(a.get("caption") for a in candidates)
            for asset in candidates:
                anchor = ""
                method = "source_page" if source_doc else ""
                marker, caption = (
                    asset.get("source_marker", ""),
                    asset.get("caption", ""),
                )
                if marker and body.count(marker) == 1:
                    anchor, method = marker, "source_marker"
                elif (
                    len(caption) >= 4
                    and body.count(caption) == 1
                    and caption_counts[caption] == 1
                ):
                    anchor, method = caption, "source_caption"
                if method:
                    db.execute(
                        "INSERT OR IGNORE INTO page_images VALUES(?,?,?,?)",
                        (page, asset["asset_id"], anchor, method),
                    )
        db.execute("PRAGMA optimize")
    return INDEX
