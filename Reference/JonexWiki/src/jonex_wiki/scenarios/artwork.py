"""Explicit opt-in compatibility for conservative native art catalogs."""

import json, tempfile
from pathlib import Path
from .store import digest
from .materials import public_unit, unit_summary


async def interpret(*, view, descriptor, units, settings, check, record, **kwargs):
    from .artwork_rules import build_catalog

    await check()
    source_root = Path(view.verified_file_path("query/resources.json")).parent.parent
    kb_root = source_root.parents[2]
    selected = {u["document_id"] for u in units}
    with tempfile.TemporaryDirectory(prefix="wiki-artwork-") as tmp:
        work = Path(tmp)
        from jonex_wiki.domain.engine.json_artifacts import load_json_artifact

        # Materialize the selected JSON through the verified shard reader;
        # copying only shard descriptors would lose their referenced files.
        for doc in selected:
            for path in (
                "sources/articles/" + doc + ".md",
                "sources/digests/" + doc + ".json",
            ):
                target = work / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(
                    json.dumps(
                        load_json_artifact(view.read_bytes, path), ensure_ascii=False
                    ).encode()
                    if path.endswith(".json")
                    else view.read_bytes(path)
                )
        if "source_contributions.json" in view.list_entries():
            (work / "source_contributions.json").write_text(
                json.dumps(
                    load_json_artifact(view.read_bytes, "source_contributions.json"),
                    ensure_ascii=False,
                )
            )

        def resolve(doc, generation, relative):
            base = (
                kb_root / "parse" / doc / ("gen-" + str(generation)) / "raw"
            ).resolve()
            target = (base / relative).resolve()
            return target if target.is_relative_to(base) else None

        catalog = build_catalog(work, resolve)

    def fragment(ref):
        body = view.read_bytes(ref["path"]).decode()
        unit = next(
            (
                u
                for u in units
                if u["modality"] == "text"
                and u.get("path") == ref["path"]
                and u["start_char"] <= ref["start_char"] < u["end_char"]
            ),
            None,
        )
        return {
            **(public_unit(unit) if unit else {}),
            **ref,
            "base_revision": view.manifest_hash,
            "text": body[ref["start_char"] : ref["end_char"]],
            "modality": "text",
            "evidence_kind": "source_article_projection",
            "locator": {
                "kind": "article_span",
                "start_char": ref["start_char"],
                "end_char": ref["end_char"],
            },
        }

    objects = []
    mapping = {}
    for entity in catalog["entities"]:
        oid = descriptor.id + "." + entity["entity_type"] + "." + entity["entity_id"]
        mapping[entity["entity_id"]] = oid
        refs = [
            r
            for r in catalog["relations"]
            if entity["entity_id"] in {r["source_entity_id"], r["target_entity_id"]}
        ]
        fragments = []
        for relation in refs:
            for ref in relation["evidence"]:
                fragments.append(fragment(ref))
        objects.append(
            {
                "object_id": oid,
                "kind": descriptor.id + "." + entity["entity_type"],
                "label": entity["label"],
                "interpretation": entity.get("description", ""),
                "fragments": fragments,
                "missing": [],
                "uncertainties": [],
                "review_status": "candidate",
                "document_ids": [entity["document_id"]],
            }
        )
    relations = [
        {
            "source": mapping[r["source_entity_id"]],
            "target": mapping[r["target_entity_id"]],
            "type": descriptor.id + "." + r["predicate"],
            "fragments": [fragment(ref) for ref in r["evidence"]],
            "review_status": "candidate",
        }
        for r in catalog["relations"]
    ]
    await check()
    return {
        "schema": "wiki.scenario-resources.v1",
        "base_revision": view.manifest_hash,
        "scenario_id": descriptor.id,
        "scenario_version": descriptor.version,
        "objects": objects,
        "relations": relations,
        "calls": [],
        "coverage": {
            "units": {u["unit_id"]: {"status": "rule_checked"} for u in units},
            "processed": len(units),
            "total": len(units),
            "processing_complete": True,
            "semantic_completeness": "not_certified",
            "limitations": ["conservative native catalog patterns only"],
            "unresolved": catalog["unresolved"],
        },
        "units": [unit_summary(u) for u in units],
    }
