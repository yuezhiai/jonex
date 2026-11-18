"""Locate citations from ordinary Agent shell output in a pinned publication.

This observes delivered shell output; it is not a search or reading tool.
Ambiguous locations remain unverified instead of triggering another model run.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from jonex_wiki.domain.engine.models.query_models import VerifiedCitation

_MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]\r\n]+\]\((<[^>\r\n]+>|[^\s()<>]+)\)")
_PATH_WITH_LINE = re.compile(r"(.+\.(?:md|txt))(?::|#L)([1-9][0-9]{0,8})", re.I)
_LINE_LOCATOR = re.compile(r"(?:line\s+([1-9][0-9]{0,8})|第\s*([1-9][0-9]{0,8})\s*行)", re.I)
_EXCERPT_ELISION = re.compile(r"[ \t]+(?:\.\.\.|…)[ \t]+")
_TABLE_ROW = re.compile(r"^\|\s*([0-9]{1,9})\s*\|")
_TABLE_BODY_IDENTITY = re.compile(r"^source_id=`([^`\r\n]+)` table_id=`([^`\r\n]+)`$", re.M)
_ROW_LOCATOR = re.compile(
    r"\brows?\s+([0-9]{1,9})(?:\s*[-–—]\s*([0-9]{1,9})|(?!\s*[-–—]))(?![0-9])"
    r"|第\s*([0-9]{1,9})(?:\s*[-–—]\s*([0-9]{1,9}))?\s*行",
    re.I,
)


def _metadata(text):
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    result = {}
    for line in text[4 : end if end >= 0 else 12000].splitlines():
        key, _, value = line.partition(":")
        try:
            result[key] = json.loads(value)
        except ValueError:
            pass
    return result


class _BundleEvidenceAccess:
    """Read registered, hash-checked files solely for evidence mapping."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self._file_proofs = {}
        manifest = self.root / "indexes/manifest.json"
        if not manifest.is_file():
            manifest = self.root / "manifest.json"
        files = json.loads(manifest.read_text())["files"]
        self.files = (
            files
            if isinstance(files, dict)
            else {v["path"]: v["sha256"] for v in files}
        )
        self.sources = json.loads(self.read_text("sources/registry.json"))["sources"]
        self.by_source = {s["source_id"]: s for s in self.sources}

    def canonical(self, name):
        if (
            not isinstance(name, str)
            or not name
            or Path(name).is_absolute()
            or ".." in Path(name).parts
        ):
            raise ValueError("PATH_MUST_BE_REGISTERED_RELATIVE")
        if name in self.files:
            return name
        if "wiki/" + name in self.files:
            return "wiki/" + name
        raise ValueError("UNREGISTERED_PATH")

    def path(self, name):
        name = self.canonical(name)
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("PATH_OUTSIDE_BUNDLE")
        from jonex_wiki.adapters.bundle_view import DirectoryBundleView

        identity = DirectoryBundleView._file_identity
        before = identity(path.stat())
        if self._file_proofs.get(name) != before:
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != self.files[name] or identity(path.stat()) != before:
                raise ValueError("CONTENT_CHANGED")
            self._file_proofs[name] = before
        return path

    def read_text(self, name):
        return self.path(name).read_text()

    def article(self, sid):
        name = "sources/articles/" + sid + ".md"
        if name in self.files:
            return name
        if not hasattr(self, "_articles"):
            self._articles = {}
            for path in self.files:
                if path.startswith("sources/digests/") and path.endswith(".json"):
                    digest = json.loads(self.read_text(path))
                    article = (
                        path.replace(
                            "sources/digests/", "sources/articles/"
                        ).removesuffix(".json")
                        + ".md"
                    )
                    if article in self.files:
                        self._articles[digest["source_id"]] = article
        return self._articles.get(sid)

    def _db(self, name):
        db = sqlite3.connect(
            self.path(name).as_uri() + "?mode=ro&immutable=1", uri=True
        )
        db.execute("PRAGMA query_only=ON")
        if hasattr(db, "enable_load_extension"):
            db.enable_load_extension(False)
        return db


class DirectFileEvidence:
    def __init__(self, root, *, revision_id, bundle_sha):
        self.access = _BundleEvidenceAccess(root)
        self.revision_id = revision_id
        self.bundle_sha = bundle_sha
        self.outputs = {}
        self.tables = {}
        self.unverified_citations = []
        self.citation_handles = {}
        self.source_paths = {
            self.access.article(sid): sid for sid in self.access.by_source
        }

    def observe_successful_request(self, messages):
        calls = {}
        for message in messages:
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except (TypeError, ValueError):
                    arguments = {}
                calls[call.get("id")] = str(arguments.get("cmd") or "")
            if message.get("role") != "tool" or not isinstance(
                message.get("content"), str
            ):
                continue
            content = message["content"]
            call_id = message.get("tool_call_id")
            output = (calls.get(call_id, ""), content)
            if call_id and self.outputs.get(call_id) == output:
                continue
            self.outputs[call_id] = output
            self._remember_full_file(content)

    def _remember_full_file(self, content):
        """Record a complete file actually received by the model, without reading for it."""
        from jonex_wiki.domain.engine.compile.table_navigation import full_table_path
        from jonex_wiki.domain.engine.query.table_reader import read_logical_table

        marker = '---\nkind: "full_logical_table"\n'
        candidates = dict.fromkeys(_TABLE_BODY_IDENTITY.findall(content))
        for part in content.split(marker)[1:]:
            metadata = _metadata(marker + part)
            tid = metadata.get("table_id")
            sids = metadata.get("source_ids") or []
            if isinstance(tid, str) and len(sids) == 1 and isinstance(sids[0], str):
                candidates[sids[0], tid] = None
        for sid, tid in candidates:
            owner = self.access.by_source.get(sid)
            if not owner or tid not in (owner.get("table_ids") or []) or not owner.get("tables"):
                continue
            path = full_table_path(sid, tid)
            if self.tables.get(tid, {}).get("full_path") == path:
                continue
            try:
                body = self.access.read_text(path)
                # A registered raw-table page can carry the same complete
                # content as the standalone file, including format and merge notes.
                table_body = body.partition("\n---\n")[2].strip()
                if body not in content and (not table_body or table_body not in content):
                    continue
                with closing(self.access._db(owner["tables"])) as db:
                    db.row_factory = sqlite3.Row
                    table = dict(read_logical_table(db, tid))
            except (OSError, ValueError, KeyError, sqlite3.Error):
                continue
            self.tables[tid] = {
                "body": body,
                "full_path": path,
                "owner": owner,
                "path": owner["tables"],
                "table": table,
            }

    def _relative(self, name):
        if not isinstance(name, str) or not name:
            return None
        path = Path(name)
        try:
            if path.is_absolute():
                name = str(path.resolve().relative_to(self.access.root))
            return self.access.canonical(name)
        except (OSError, ValueError):
            return None

    def _source_ids(self, path, quote):
        if path in self.source_paths:
            return [self.source_paths[path]]
        if path.startswith("articles/") and "catalog.sqlite" in self.access.files:
            try:
                with closing(self.access._db("catalog.sqlite")) as db:
                    db.row_factory = sqlite3.Row
                    row = db.execute(
                        "SELECT * FROM objects WHERE path=? AND kind IN ('section','synthesis') LIMIT 1",
                        (path,),
                    ).fetchone()
                    if row:
                        ids = json.loads(row["source_ids"] or "[]")
                        if len(ids) <= 1:
                            return ids
                        # An article can combine several sources. Use a unique
                        # quoted block's source, never choose one arbitrarily.
                        if row["kind"] == "section":
                            sections = [row["id"]]
                        else:
                            sections = [
                                r[0]
                                for r in db.execute(
                                    "SELECT id FROM objects WHERE kind='section' AND parent=?",
                                    (row["id"],),
                                )
                            ]
                        matches = set()
                        for (block,) in db.execute(
                            "SELECT payload FROM prose_blocks WHERE section IN (SELECT value FROM json_each(?))",
                            (json.dumps(sections),),
                        ):
                            value = json.loads(block)
                            if quote in (value.get("text") or ""):
                                matches.update(value.get("source_ids") or [])
                        return sorted(matches) if len(matches) == 1 else []
            except (OSError, ValueError, KeyError, sqlite3.Error):
                return []
        try:
            meta = _metadata(self.access.read_text(path))
        except (OSError, ValueError, UnicodeError):
            return []
        scope = meta.get("scope") or {}
        return [
            sid
            for sid in (meta.get("source_ids") or scope.get("source_ids") or [])
            if sid in self.access.by_source
        ]

    def _seen(self, path, quote):
        absolute = str(self.access.root / path)
        return any(
            quote in output
            and (
                path in command
                or absolute in command
                or path in output
                or absolute in output
            )
            for command, output in self.outputs.values()
        )

    @staticmethod
    def _position(text, quote, locator):
        positions = []
        pos = text.find(quote)
        while pos >= 0:
            positions.append(pos)
            pos = text.find(quote, pos + 1)
        if len(positions) > 1:
            match = re.search(r"(?:line|第)\s*(\d+)", str(locator or ""), re.IGNORECASE)
            if match:
                line = int(match.group(1))
                positions = [p for p in positions if text.count("\n", 0, p) + 1 == line]
        return positions[0] if len(positions) == 1 else None

    @staticmethod
    def _elided_source_span(text, quote, locator):
        """Resolve an abbreviated quote against one reported original line."""
        parts = _EXCERPT_ELISION.split(quote.strip())
        if len(parts) < 2 or len(parts) > 8 or any(not part for part in parts):
            return None
        reported = locator.strip() if isinstance(locator, str) else ""
        line = _LINE_LOCATOR.search(reported)
        if line is None or line.end() != len(reported):
            return None
        number = int(line[1] or line[2])
        lines = text.split("\n", number)
        if len(lines) < number:
            return None
        source_line = lines[number - 1]
        prefix = parts[0]
        start = source_line.find(prefix)
        if start < 0 or source_line.find(prefix, start + 1) >= 0:
            return None
        end = start + len(prefix)
        for part in parts[1:]:
            pos = source_line.find(part, end)
            if pos < 0 or source_line.find(part, pos + 1) >= 0:
                return None
            end = pos + len(part)
        return source_line[start:end], sum(len(line) + 1 for line in lines[:number - 1]) + start

    def _text_citation(self, item):
        path = self._relative(item.get("path"))
        if not path or not path.endswith((".md", ".txt")):
            return None
        quote = item.get("quote")
        locator = item.get("locator")
        has_quote = isinstance(quote, str) and bool(quote.strip())
        original = path in self.source_paths or path.startswith("wiki/source-articles/")
        line_match = _LINE_LOCATOR.fullmatch(locator.strip()) if isinstance(locator, str) else None
        if not has_quote and (not original or not line_match):
            return None
        table_id = re.search(r"table-[a-zA-Z0-9_-]+", path)
        direct_table = path.startswith("full-tables/")
        if table_id and not direct_table and table_id.group() not in self.tables:
            return None
        if has_quote and table_id and table_id.group() in self.tables:
            entry = self.tables[table_id.group()]
            # The model may cite a Markdown table page after receiving the
            # identical row from the complete logical table. Point the
            # citation at that actually delivered native row instead.
            native = self._table_citation(
                {
                    "path": entry["path"],
                    "quote": quote,
                    "locator": table_id.group() + " " + str(locator or ""),
                }
            )
            if native is not None:
                return native
        try:
            text = self.access.read_text(path)
        except (OSError, ValueError, UnicodeError):
            return None
        if not has_quote:
            number = int(line_match.group(1) or line_match.group(2))
            lines = text.split("\n", number)
            if len(lines) < number or not lines[number - 1].strip():
                return None
            quote = lines[number - 1]
            locator = "line " + str(number)
        start = self._position(text, quote, locator)
        if start is None and original and (span := self._elided_source_span(text, quote, locator)):
            quote, start = span
        if start is None or not self._seen(path, quote):
            return None
        ids = self._source_ids(path, quote)
        if len(ids) != 1 or ids[0] not in self.access.by_source:
            return None
        sid = ids[0]
        doc = self.access.by_source[sid].get("document_id") or sid
        left = text.rfind("\n", 0, start) + 1
        right = text.find("\n", start)
        line = text[left : right if right >= 0 else len(text)]
        if (
            not direct_table
            and line.count("|") >= 2
            and not any(
                t["owner"]["source_id"] == sid and quote in t["body"]
                for t in self.tables.values()
            )
        ):
            # A source chapter can embed only a preview of a native table.
            # A different table from the same source does not complete it.
            return None
        ident = (
            "file_"
            + hashlib.sha256(
                (
                    self.bundle_sha + "\0" + path + "\0" + str(start) + "\0" + quote
                ).encode()
            ).hexdigest()[:32]
        )
        compiled = path.startswith("articles/")
        ref = {
            "kind": "compiled_passage" if compiled else "source_span",
            "path": path,
            "spans": [[start, start + len(quote)]],
            "revision_id": self.revision_id,
            "bundle_sha": self.bundle_sha,
            "reported_locator": item.get("locator") or "",
            "original_actually_read": original,
        }
        if compiled:
            ref["verification"] = (
                "file_location_only; compiled factual accuracy not established by this check"
            )
        return VerifiedCitation(
            claim_id=ident,
            passage_id=ident,
            source_id=sid,
            document_id=doc,
            chunk_id=ident,
            excerpt=quote,
            page_path=path,
            evidence_ref=ref,
        )

    def _table_citation(self, item):
        ids = set(re.findall(r"table-[a-zA-Z0-9_-]+", str(item.get("locator") or "")))
        if len(ids) != 1:
            return None
        tid = next(iter(ids))
        entry = self.tables.get(tid)
        quote = item.get("quote")
        if (
            not entry
            or self._relative(item.get("path")) != entry["path"]
            or not isinstance(quote, str)
            or not quote
        ):
            return None
        lines = [line for line in entry["body"].splitlines() if quote in line]
        if len(lines) > 1:
            # Repeated values such as "yes" or "pending" can still identify a
            # native row when the model supplied that row explicitly.
            row_hint = re.search(
                r"(?:\brow\s*(\d+)|第\s*(\d+)\s*行)",
                str(item.get("locator") or ""),
                re.IGNORECASE,
            )
            if row_hint:
                wanted = int(row_hint.group(1) or row_hint.group(2))
                lines = [
                    line
                    for line in lines
                    if (match := re.match(r"^\|\s*(\d+)\s*\|", line))
                    and int(match.group(1)) == wanted
                ]
        if len(lines) != 1:
            return None
        match = re.match(r"^\|\s*(\d+)\s*\|", lines[0])
        if not match:
            return None
        return self._native_row_citation(tid, int(match.group(1)), quote)

    def _table_row_group(self, item):
        """Verify row text or resolve explicit coordinates in the delivered table."""
        path = self._relative(item.get("path"))
        if not path:
            return []
        if path.startswith("full-tables/"):
            tid = Path(path).stem
            entry = self.tables.get(tid)
            if not entry or path != entry["full_path"]:
                return []
        elif path.endswith(".sqlite"):
            ids = set(re.findall(r"table-[a-zA-Z0-9_-]+", str(item.get("locator") or "")))
            if len(ids) != 1:
                return []
            tid = next(iter(ids))
            entry = self.tables.get(tid)
            if not entry or path != entry["path"]:
                return []
        elif path.startswith("wiki/raw-tables/"):
            table_id = re.search(r"table-[a-zA-Z0-9_-]+", path)
            tid = table_id[0] if table_id else None
            entry = self.tables.get(tid)
            if not entry or self._source_ids(path, "") != [entry["owner"]["source_id"]]:
                return []
        else:
            return []
        printed = {int(match[1]): line for line in entry["body"].splitlines()
                   if (match := _TABLE_ROW.match(line))}
        quote = item.get("quote")
        if isinstance(quote, str) and quote.lstrip().startswith("|"):
            rows = {}
            for line in quote.splitlines():
                match = _TABLE_ROW.match(line.strip())
                if not match:
                    return []
                row = int(match[1])
                original = printed.get(row)
                # The closing Markdown delimiter can be omitted by the model.
                if original is None or line.strip() not in (original, original.removesuffix("|").rstrip()):
                    return []
                rows[row] = original
            return [self._native_row_citation(tid, row, line) for row, line in rows.items()]

        ranges = {}
        for match in _ROW_LOCATOR.finditer(str(item.get("locator") or "")):
            start = int(match[1] or match[3])
            end = int(match[2] or match[4] or start)
            if end < start or end - start >= len(printed):
                return []
            lines = [printed.get(row) for row in range(start, end + 1)]
            if not all(lines):
                return []
            ranges[start, end] = "\n".join(lines)
        return [self._native_row_citation(tid, start, text, end=end)
                for (start, end), text in ranges.items()]

    def _native_row_citation(self, tid, row, quote, *, end=None):
        entry = self.tables[tid]
        table = entry["table"]
        owner = entry["owner"]
        sid = owner["source_id"]
        position = str(row) if end is None else f"{row}:{end}"
        ident = (
            "table_"
            + hashlib.sha256(
                (self.bundle_sha + "\0" + tid + "\0" + position + "\0" + quote).encode()
            ).hexdigest()[:32]
        )
        ref = {
            "kind": "table_range",
            "path": entry["path"],
            "table_id": tid,
            "sheet_id": table["sheet_id"],
            "row_start": row,
            "row_end": row if end is None else end,
            "column_start": table["column_start"],
            "column_end": table["column_end"],
            "revision_id": self.revision_id,
            "bundle_sha": self.bundle_sha,
        }
        if end is not None:
            ref["verification"] = "delivered_table_row_coordinates"
        return VerifiedCitation(
            claim_id=ident,
            passage_id=ident,
            source_id=sid,
            document_id=owner.get("document_id") or sid,
            chunk_id=tid,
            excerpt=quote,
            page_path="wiki/tables/" + tid + ".md",
            evidence_ref=ref,
        )

    def citations(self, evidence, *, answer_text=""):
        citations = []
        issues = []
        self.unverified_citations = []
        self.citation_handles = {}
        if not isinstance(evidence, list) or not evidence:
            # Recover explicit Markdown file/line references only after the
            # same registered-file and actual-delivery checks as other items.
            links = dict.fromkeys(
                match.groups()
                for link in _MARKDOWN_LINK.finditer(answer_text)
                if (match := _PATH_WITH_LINE.fullmatch(link[1].removeprefix("<").removesuffix(">")))
            )
            evidence = [{"path": path, "locator": "line " + line} for path, line in links]
        for number, item in enumerate(evidence):
            if not isinstance(item, dict):
                issues.append(f"{number}:INVALID_EVIDENCE")
                continue
            citation = (
                self._table_citation(item)
                if str(item.get("path") or "").endswith(".sqlite")
                else self._text_citation(item)
            )
            if citation is None:
                matched = self._table_row_group(item)
            elif citation.evidence_ref["kind"] == "source_span" and citation.page_path.startswith("wiki/raw-tables/"):
                matched = self._table_row_group(item) or [citation]
            else:
                matched = [citation]
            if not matched:
                issues.append(f"{number}:LOCATION_OR_DELIVERY_NOT_CONFIRMED")
                path = item.get("path")
                locator = item.get("locator")
                if isinstance(path, str) and path:
                    self.unverified_citations.append(
                        {
                            "reported_path": path,
                            "reported_locator": locator if locator is not None else "",
                            "reported_quote": item.get("quote")
                            if isinstance(item.get("quote"), str)
                            else "",
                            "bundle_path": self._relative(path),
                            "status": "unverified",
                            "reason": "location_or_delivery_not_confirmed",
                            "revision_id": self.revision_id,
                            "bundle_sha": self.bundle_sha,
                        }
                    )
                continue
            handles = []
            for citation in matched:
                citation.handle = f"[E{len(citations) + 1}]"
                handles.append(citation.handle)
                citations.append(citation)
            self.citation_handles[number + 1] = "".join(handles)
        return citations, issues

    def remap_answer_citations(self, answer):
        """Keep model-written links only when their evidence entry verified.

        The model numbers the original evidence array; dropped entries would
        otherwise cause a later verified source to appear against the wrong
        claim. An unverified marker must never become a clickable citation.
        """
        return re.sub(
            r"\[E([0-9]+)\]",
            lambda match: self.citation_handles.get(int(match.group(1)), ""),
            answer,
        )
