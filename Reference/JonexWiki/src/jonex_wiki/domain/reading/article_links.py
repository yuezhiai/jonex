"""Resolve authored links from their published location and check draft paths."""

from __future__ import annotations

import json
from pathlib import Path
import posixpath
import re
import sys
from urllib.parse import unquote, urlsplit


MARKDOWN_LINK = re.compile(r'(?<!!)\[([^\]\n]*)\]\(([^\s)]+)(?:\s+"[^"]*")?\)')


def resolve_link(owner: str, value: str) -> str | None:
    """Resolve a relative Markdown link within the publication namespace."""
    try:
        parsed = urlsplit(value.strip().strip("<>"))
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
        return None
    path = unquote(parsed.path)
    if "\\" in path or "\x00" in path:
        return None
    target = posixpath.normpath(posixpath.join(posixpath.dirname(owner), path))
    if target == ".." or target.startswith("../"):
        return None
    return target


def authored_link_targets(path, body):
    for link in MARKDOWN_LINK.finditer(body):
        target = resolve_link(path, link.group(2))
        if target:
            yield target


def check_draft_links(bundle: Path, output: Path, destination: str) -> dict:
    """Check relative links against registered files and drafts in this batch."""
    bundle = bundle.resolve()
    entries = json.loads((bundle / "indexes/manifest.json").read_text())["files"]
    drafts = {
        destination + path.relative_to(output).as_posix(): path
        for path in output.rglob("*.md")
        if path.is_file() and not path.is_symlink()
    }
    checked = 0
    unresolved = []
    unresolved_total = 0
    for owner, draft in sorted(drafts.items()):
        for link in MARKDOWN_LINK.finditer(draft.read_text()):
            target = resolve_link(owner, link.group(2))
            if target is None:
                continue
            checked += 1
            if target in drafts or target == destination + "index.md":
                continue
            path = (bundle / target).resolve()
            if target in entries and path.is_relative_to(bundle) and path.is_file():
                continue
            unresolved_total += 1
            if len(unresolved) < 30:
                unresolved.append({
                    "article": draft.relative_to(output).as_posix(),
                    "label": link.group(1), "target": target,
                })
    return {
        "checked": checked, "unresolved": unresolved,
        "unresolved_total": unresolved_total,
    }


if __name__ == "__main__":
    publication, output, destination = sys.argv[1:]
    result = check_draft_links(Path(publication), Path(output), destination)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
