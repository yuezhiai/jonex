"""Optional source-image tools. Lexical candidates never become visual evidence."""

import asyncio
import copy
import hashlib
import json
import tempfile
import time
from datetime import UTC, datetime

from jonex_wiki.domain.engine.util.model_io import _model_complete
from jonex_wiki.domain.reading.image_inspection import (
    prepare_image,
    validate_observation,
)
from jonex_wiki.domain.reading.image_service import find_images
from jonex_wiki.domain.reading.visual_inspection import OUTPUT_SCHEMA
from jonex_wiki.kernel.runtime_env import current_env

IMAGE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "image_find",
            "description": "Find source pictures using existing captions, OCR and nearby text. Cite a returned image metadata handle to include that original picture in the search answer without calling a vision model. Metadata supports image identity, location and supplied caption only, never depicted facts. Use image_inspect when visual content must be understood. Pictures without text signals may be missed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "document_id": {"type": "string"},
                    "page_path": {"type": "string"},
                    "cursor": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_inspect",
            "description": "Actually read one selected source image with a vision model. Use only when the answer needs visual content. Returns a citable candidate observation plus an actual-image read receipt; report uncertainties. Up to two reads per query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_id": {"type": "string"},
                    "question": {"type": "string"},
                    "region": {
                        "type": "array",
                        "items": {"type": "number", "minimum": 0, "maximum": 1},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                },
                "required": ["asset_id", "question"],
                "additionalProperties": False,
            },
        },
    },
]


def available_tools(state):
    view = state.get("bundle_view") if state else None
    if view is None or "indexes/images.sqlite" not in view.list_entries():
        return []
    return (
        IMAGE_TOOLS
        if current_env().get("QUERY_IMAGE_INSPECTION_ENABLED") == "1"
        else IMAGE_TOOLS[:1]
    )


async def execute_image_tool(
    name,
    args,
    state,
    model,
    routing,
    *,
    usage_acc=None,
    spans=None,
    parent_span_id=None,
):
    view = state.get("bundle_view")
    if view is None or name not in {
        tool["function"]["name"] for tool in available_tools(state)
    }:
        return {"status": "unavailable"}, [], {}
    if name == "image_find":
        result = await asyncio.to_thread(
            find_images, view, {**args, "limit": args.get("limit", 10)}
        )
        # No local storage paths or binary payloads in the agent context.
        fields = {
            "asset_id",
            "document_id",
            "source_name",
            "caption",
            "ocr_text",
            "context",
            "description",
            "source_locator",
            "context_status",
            "bundle_sha",
            "usage",
            "score_kind",
            "score",
        }
        result["items"] = [
            {k: v for k, v in item.items() if k in fields} for item in result["items"]
        ]
        for item in result["items"]:
            for field, limit in {"caption": 512, "ocr_text": 1024, "context": 768, "description": 512}.items():
                if isinstance(item.get(field), str):
                    item[field] = item[field][:limit]
        handles, refs = [], {}
        for image in result["items"][:3]:
            claim = "image_metadata_" + image["asset_id"]
            metadata = {
                "source_name": image["source_name"],
                "supplied_caption": image.get("caption") or "",
                "supplied_ocr": str(image.get("ocr_text") or "")[:1024],
                "display_available": True,
                "image_content_read": False,
            }
            handle = {
                "claim_id": claim,
                "passage_id": claim,
                "source_id": image["document_id"],
                "document_id": image["document_id"],
                "chunk_id": image["asset_id"],
                "page_path": "sources/articles/" + image["document_id"] + ".md",
                "excerpt": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            }
            handles.append(handle)
            refs[claim] = {
                "kind": "image_metadata",
                "revision": view.manifest_hash,
                "revision_id": view.revision_id,
                "asset_id": image["asset_id"],
                "document_id": image["document_id"],
                "locator": image["source_locator"],
                "evidence_kind": "source_image_metadata",
                "usage": "illustration",
                "display_available": True,
                "actual_image_delivered": False,
            }
        result.update(
            status="found" if result["items"] else "not_found",
            evidence=copy.deepcopy(handles),
            evidence_limit="Metadata handles support source identity and supplied captions, not image content.",
        )
        return result, handles, refs
    key = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
    cache = state.setdefault("image_observations", {})
    if key in cache:
        result, handles, refs = copy.deepcopy(cache[key])
        return {**result, "cached": True}, handles, refs
    if len(cache) >= 2:
        return {"status": "image_read_budget_exhausted"}, [], {}

    def prepare():
        with tempfile.TemporaryDirectory(prefix="wiki-image-read-") as folder:
            return prepare_image(
                view,
                args["asset_id"],
                args["question"],
                folder,
                region=args.get("region"),
            )

    receipt, messages = await asyncio.to_thread(prepare)
    request_routing = dict(routing or {})
    if current_env().get("QUERY_IMAGE_MODEL"):
        request_routing["model"] = current_env()["QUERY_IMAGE_MODEL"]
    started_dt, started = datetime.now(UTC), time.monotonic()
    raw = await _model_complete(
        model,
        messages,
        OUTPUT_SCHEMA,
        max_tokens=2048,
        tools=None,
        thinking={"type": "disabled"},
        scene="jonex_wiki_query_image",
        **request_routing,
    )
    from jonex_wiki.domain.engine.util.model_io import _merge_usage, _record_model_span

    account = (
        usage_acc if usage_acc is not None else {"input_tokens": 0, "output_tokens": 0}
    )
    if spans is not None:
        _record_model_span(
            spans,
            raw,
            request_routing.get("model"),
            started_dt,
            int((time.monotonic() - started) * 1000),
            account,
            title="read source image",
            parent_id=parent_span_id,
        )
    else:
        _merge_usage(account, raw)
    observation = validate_observation(raw)
    read_id = (
        "image_read_"
        + hashlib.sha256(
            json.dumps([receipt, observation], sort_keys=True).encode()
        ).hexdigest()[:32]
    )
    receipt = {
        **receipt,
        "read_id": read_id,
        "actual_image_delivered": True,
        "model": request_routing.get("model") or current_env().get("LLM_MODEL", ""),
    }
    excerpt = (
        observation["observations"] + "\n" + observation["visible_text"]
    ).strip()[:1200]
    if not excerpt:
        return (
            {
                "status": "no_visual_observation",
                "receipt": receipt,
                "result": observation,
            },
            [],
            {},
        )
    claim = "image_observation_" + read_id
    handle = {
        "claim_id": claim,
        "passage_id": claim,
        "source_id": receipt["document_id"],
        "document_id": receipt["document_id"],
        "chunk_id": args["asset_id"],
        "excerpt": excerpt,
        "page_path": "sources/articles/" + receipt["document_id"] + ".md",
    }
    refs = {
        claim: {
            **receipt,
            "kind": "image_observation",
            "revision_id": view.revision_id,
            "uncertainties": observation["uncertainties"],
        }
    }
    result = {
        "status": "observed",
        "receipt": receipt,
        "result": observation,
        "evidence": [handle],
        "usage": "visual_evidence",
        "cached": False,
    }
    cache[key] = (result, [handle], refs)
    # Citation binding edits delivered payloads; cache source IDs must remain
    # intact when the same image is read twice in one traversal.
    return copy.deepcopy(cache[key])
