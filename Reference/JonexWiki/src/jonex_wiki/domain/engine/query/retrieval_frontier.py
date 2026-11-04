"""Direct and graph-assisted entry policies. Candidates still need source reading."""

import hashlib
import json
import re
from pathlib import Path
from jonex_wiki.domain.engine.compile.navigation_graph import PATH


def navigation_context(view, paths, limit=3):
    if view is None or PATH not in view.list_entries():
        return []
    graph = json.loads(view.read_bytes(PATH))
    result = []
    seen = set(paths)
    for path in paths:
        for parent in graph["parents"].get(path, []):
            if parent not in seen and parent != "index.md":
                seen.add(parent)
                result.append(parent)
                if len(result) >= limit:
                    return result
    return result


def semantic_context(view, question, limit=4):
    if view is None:
        return [], {"reason": "no_bundle"}
    # Derivation is from a verified file in the pinned publication, not a caller path.
    pub = Path(view.verified_file_path("query/resources.json")).parent.parent
    folder = pub.parents[2] / "retrieval" / "semantic"
    pointer = folder / (view.manifest_hash + ".json")
    if not pointer.is_file():
        return [], {"reason": "index_unavailable"}
    header = json.loads(pointer.read_text())
    sha = header.get("sha256", "")
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("SEMANTIC_INDEX_ID_INVALID")
    raw = (folder / "versions" / (sha + ".json")).read_bytes()
    if len(raw) > 16 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError("SEMANTIC_INDEX_HASH_MISMATCH")
    index = json.loads(raw)
    if (index["base_revision"], index["tenant_id"], index["kb_id"]) != (
        view.manifest_hash,
        view.tenant_id,
        view.context_id,
    ):
        raise ValueError("SEMANTIC_INDEX_SCOPE_MISMATCH")
    q = question.casefold()
    seeds = set()
    matches = {}
    for entity in index["entities"]:
        for alias in [entity["label"], *entity.get("aliases", [])]:
            alias = alias.casefold().strip()
            if len(alias) < 2 or alias.isdecimal():
                continue
            # Avoid Alpha matching alphabet and code 1001 matching 10010.
            found = (
                bool(
                    re.search(
                        r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])", q
                    )
                )
                if alias.isascii()
                else alias in q
            )
            if found:
                matches.setdefault(alias, []).append(entity["id"])
                seeds.add(entity["id"])
    initial_seed_count = len(seeds)
    seeds = set(sorted(seeds)[:12])
    selected = []
    paths = []
    frontier = set(seeds)
    for hop in range(2):
        following = set()
        for relation in index["relations"]:
            if (
                relation["source"] not in frontier
                and relation["target"] not in frontier
            ):
                continue
            if relation["relation_id"] not in selected:
                selected.append(relation["relation_id"])
            if relation["page_path"] not in paths:
                paths.append(relation["page_path"])
            following.update([relation["source"], relation["target"]])
            if len(paths) >= limit:
                break
        frontier = following - seeds
        seeds.update(following)
        if len(paths) >= limit or not frontier:
            break
    return paths[:limit], {
        "artifact": sha,
        "seed_count": initial_seed_count,
        "expanded_count": len(seeds),
        "ambiguous_aliases": {k: v for k, v in matches.items() if len(set(v)) > 1},
        "relation_ids": selected[:20],
        "candidate_paths": paths[:limit],
        "support": "discovery_only",
        "coverage": index["coverage"],
    }
