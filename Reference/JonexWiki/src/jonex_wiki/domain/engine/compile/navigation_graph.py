"""Renderer-owned directory graph; deterministic and rebuildable."""

import json
import re
from pathlib import Path

PATH = "indexes/navigation_graph.json"


def build(pub):
    root = pub / "wiki"
    adjacency = {}
    broken = []
    for file in sorted(root.rglob("index.md")):
        parent = file.relative_to(root).as_posix()
        children = []
        for match in re.finditer(
            r"(?m)^- \[\[([^\]|#]+)(?:\|[^\]]*)?\]\]", file.read_text()
        ):
            target = match.group(1)
            path = (root / target).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                broken.append({"from": parent, "to": target})
                continue
            children.append(target)
        adjacency[parent] = sorted(set(children))
    parents = {}
    for parent, children in adjacency.items():
        for child in children:
            parents.setdefault(child, []).append(parent)
    result = {
        "schema": "wiki.navigation-graph.v1",
        "adjacency": adjacency,
        "parents": parents,
        "broken": broken,
    }
    path = pub / PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return result
