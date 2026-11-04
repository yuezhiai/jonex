"""Optional scenario jobs. Generic queries never consult this service."""

from __future__ import annotations
import asyncio
import json
import re
import uuid
from pydantic import BaseModel, ConfigDict, Field
from jonex_wiki.adapters.bundle_view import DirectoryBundleView
from jonex_wiki.kernel.errors import ConflictError, NotFoundError, ValidationError
from .registry import ScenarioRegistry
from .store import (
    digest,
    read_state,
    write_state,
    write_output,
    read_output,
    event,
    scope_root,
    now,
)


class ProcessingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_calls: int = Field(default=8, ge=1, le=64)
    max_images: int = Field(default=8, ge=0, le=64)
    batch_chars: int = Field(default=24000, ge=2000, le=60000)
    output_tokens: int = Field(default=4096, ge=512, le=16384)
    model: str = Field(default="", max_length=200)
    visual_model: str = Field(default="", max_length=200)


class Configure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0, strict=True)
    enabled: bool = Field(strict=True)
    strategy: str = "on_demand"
    document_ids: list[str] = Field(default_factory=list, max_length=1000)
    config: ProcessingConfig = Field(default_factory=ProcessingConfig)


class ScenarioService:
    def __init__(self, application, registry=None):
        self.app = application
        self.registry = registry or ScenarioRegistry()
        self.jobs = {}
        self.semaphore = asyncio.Semaphore(2)

    def implementation_identity(self, descriptor):
        from pathlib import Path

        return digest(
            [
                descriptor.model_dump(),
                {
                    p.name: digest(p.read_text())
                    for p in Path(__file__).parent.glob("*.py")
                },
            ]
        )

    def view(self, tenant, kb, revision=""):
        from .store import scope_root

        scope_root(self.app.layout, tenant, kb)
        if revision and not re.fullmatch("[0-9a-f]{64}", revision):
            raise ValidationError("SCENARIO_BASE_REVISION_INVALID")
        pub = (
            self.app.layout.versions_dir(tenant, kb, revision)
            if revision
            else self.app.layout.resolve_published(tenant, kb)
        )
        if not pub or not pub.is_dir():
            raise NotFoundError("SCENARIO_PUBLICATION_NOT_FOUND")
        view = DirectoryBundleView(tenant_id=tenant, context_id=kb, revision_dir=pub)
        if (view.tenant_id, view.context_id, view.manifest_hash) != (
            tenant,
            kb,
            pub.name,
        ):
            raise ValidationError("SCENARIO_PUBLICATION_SCOPE_MISMATCH")
        return view

    async def list(self, tenant, kb):
        async with self.app.layout.version_lock(tenant, kb):
            state = read_state(self.app.layout, tenant, kb)
            pub = self.app.layout.resolve_published(tenant, kb)
            revision = pub.name if pub else None
            changed = False
            # Recovery is explicit and does not silently charge another model
            # run. A crashed worker must not leave the switch stuck at running.
            for sid, current in state["scenarios"].items():
                if (
                    current.get("status") in {"running", "queued"}
                    and current.get("job_id") not in self.jobs
                ):
                    current.update(
                        status="failed",
                        error="SCENARIO_PROCESS_INTERRUPTED: rerun explicitly",
                    )
                    event(self.app.layout, tenant, kb, current["job_id"], "interrupted")
                    changed = True
            if changed:
                state["version"] += 1
                write_state(self.app.layout, tenant, kb, state)
            items = []
            for descriptor in self.registry.list():
                config = state["scenarios"].get(
                    descriptor["id"], {"enabled": False, "status": "disabled"}
                )
                status = config.get("status")
                if config.get("enabled") and config.get("base_revision") != revision:
                    status = "stale"
                if (
                    config.get("enabled")
                    and config.get("scenario_version") != descriptor["version"]
                ):
                    status = "stale"
                if (
                    config.get("enabled")
                    and config.get("implementation_identity")
                    and config["implementation_identity"]
                    != self.implementation_identity(self.registry.get(descriptor["id"]))
                ):
                    status = "stale"
                operations = [
                    op
                    for op in descriptor["operations"]
                    if op in {"interpret", "materials", "material", "annotate"}
                    or config.get("result_revision")
                ]
                items.append(
                    {
                        **descriptor,
                        **config,
                        "status": status,
                        "available_operations": operations
                        if config.get("enabled") and status == "ready"
                        else [],
                    }
                )
            documents = []
            if pub:
                view = self.view(tenant, kb, revision)
                catalog = json.loads(view.read_bytes("query/resources.json"))
                documents = [
                    {"document_id": o["document_id"], "label": o["label"]}
                    for o in catalog["objects"]
                    if o["kind"] == "document"
                ]
            return {
                "schema": state["schema"],
                "version": state["version"],
                "base_revision": revision,
                "items": items,
                "documents": documents,
                "processing_config_schema": ProcessingConfig.model_json_schema(),
            }

    async def configure(self, tenant, kb, scenario_id, options, actor):
        if not actor:
            raise ValidationError("SCENARIO_ACTOR_REQUIRED")
        try:
            options = Configure.model_validate(options)
        except ValueError as exc:
            raise ValidationError("SCENARIO_CONFIG_INVALID: " + str(exc)) from exc
        descriptor = self.registry.get(scenario_id)
        if options.strategy not in descriptor.strategies:
            raise ValidationError("SCENARIO_STRATEGY_UNKNOWN")
        async with self.app.layout.version_lock(tenant, kb):
            state = read_state(self.app.layout, tenant, kb)
            if options.expected_version != state["version"]:
                raise ConflictError("SCENARIO_CONFIG_VERSION_CHANGED")
            for dependency in descriptor.dependencies:
                if options.enabled and not state["scenarios"].get(dependency, {}).get(
                    "enabled"
                ):
                    raise ValidationError("SCENARIO_DEPENDENCY_DISABLED")
            previous = state["scenarios"].get(scenario_id, {})
            if not options.enabled:
                for sid, config in state["scenarios"].items():
                    if (
                        config.get("enabled")
                        and scenario_id in self.registry.get(sid).dependencies
                    ):
                        raise ConflictError("SCENARIO_DEPENDENT_ENABLED")
                config = {
                    **previous,
                    "enabled": False,
                    "status": "disabled",
                    "job_id": None,
                    "actor": actor,
                }
            else:
                view = self.view(tenant, kb)
                # Validate requested ownership without loading the processor.
                from .materials import collect

                await asyncio.to_thread(
                    collect, view, self.app.layout, options.document_ids
                )
                config = {
                    "enabled": True,
                    "status": "ready" if options.strategy == "on_demand" else "queued",
                    "strategy": options.strategy,
                    "document_ids": sorted(set(options.document_ids)),
                    "config": options.config.model_dump(),
                    "base_revision": view.manifest_hash,
                    "scenario_version": descriptor.version,
                    "implementation_identity": self.implementation_identity(descriptor),
                    "job_id": uuid.uuid4().hex,
                    "actor": actor,
                }
                self.app.layout.set_version_pin(
                    tenant, kb, "scenario:" + scenario_id, view.manifest_hash
                )
            state["version"] += 1
            config["config_version"] = state["version"]
            state["scenarios"][scenario_id] = config
            write_state(self.app.layout, tenant, kb, state)
        old_job = previous.get("job_id")
        if old_job in self.jobs:
            self.jobs[old_job].cancel()
        if config.get("enabled") and options.strategy != "on_demand":
            self.schedule(tenant, kb, scenario_id, config)
        return await self.list(tenant, kb)

    def schedule(self, tenant, kb, scenario_id, config, unit_ids=None):
        task = asyncio.create_task(
            self.run(tenant, kb, scenario_id, dict(config), unit_ids)
        )
        self.jobs[config["job_id"]] = task
        task.add_done_callback(lambda done: self.jobs.pop(config["job_id"], None))

    async def check(self, tenant, kb, scenario_id, config):
        current = read_state(self.app.layout, tenant, kb)["scenarios"].get(
            scenario_id, {}
        )
        if (
            not current.get("enabled")
            or current.get("job_id") != config["job_id"]
            or current.get("config_version") != config["config_version"]
        ):
            raise asyncio.CancelledError("SCENARIO_CONFIGURATION_REPLACED")
        if config.get("implementation_identity") != self.implementation_identity(
            self.registry.get(scenario_id)
        ):
            raise asyncio.CancelledError("SCENARIO_IMPLEMENTATION_CHANGED")
        published = self.app.layout.resolve_published(tenant, kb)
        if not published or published.name != config["base_revision"]:
            raise asyncio.CancelledError("SCENARIO_BASE_CHANGED")

    async def run(self, tenant, kb, scenario_id, config, unit_ids=None):
        layout = self.app.layout

        async def check():
            await self.check(tenant, kb, scenario_id, config)

        async def record(name, **values):
            event(layout, tenant, kb, config["job_id"], name, **values)

        try:
            async with self.semaphore:
                await check()
                async with layout.version_lock(tenant, kb):
                    await check()
                    state = read_state(layout, tenant, kb)
                    state["scenarios"][scenario_id]["status"] = "running"
                    write_state(layout, tenant, kb, state)
                view = self.view(tenant, kb, config["base_revision"])
                from .materials import collect

                units = await asyncio.to_thread(
                    collect, view, layout, config["document_ids"]
                )
                if unit_ids is not None:
                    if set(unit_ids) - {u["unit_id"] for u in units}:
                        raise ValidationError("SCENARIO_UNIT_OUT_OF_SCOPE")
                    units = [u for u in units if u["unit_id"] in unit_ids]
                processor, descriptor = self.registry.load(scenario_id)
                await record(
                    "started",
                    base_revision=view.manifest_hash,
                    scenario_id=scenario_id,
                    scenario_version=descriptor.version,
                    strategy=config["strategy"],
                    unit_ids=[u["unit_id"] for u in units],
                )
                from pathlib import Path
                import inspect

                processor_file = inspect.getsourcefile(processor)
                processor_identity = digest(
                    {
                        "host": {
                            p.name: digest(p.read_text())
                            for p in Path(__file__).parent.glob("*.py")
                        },
                        "processor": digest(Path(processor_file).read_text())
                        if processor_file
                        else descriptor.processor,
                    }
                )
                key = digest(
                    [
                        view.manifest_hash,
                        descriptor.model_dump(),
                        processor_identity,
                        config["strategy"],
                        config["document_ids"],
                        config["config"],
                        getattr(self.app.settings, "llm_model", ""),
                        getattr(self.app.settings, "visual_inspection_model", ""),
                        [u["unit_id"] for u in units],
                    ]
                )
                cache = scope_root(layout, tenant, kb) / "cache" / (key + ".json")
                if cache.is_file():
                    pointer = json.loads(cache.read_text())
                    result = read_output(layout, tenant, kb, pointer["revision"])
                    await record("cache_hit", revision=pointer["revision"])
                else:
                    result = await processor(
                        view=view,
                        descriptor=descriptor,
                        units=units,
                        gateway=self.app.gw,
                        settings=self.app.settings,
                        strategy=config["strategy"],
                        config=config["config"],
                        check=check,
                        record=record,
                    )
                    await check()
                    result["configuration_identity"] = key
                    result["type_registry"] = {
                        "kinds": [descriptor.id + "." + k for k in descriptor.kinds],
                        "relations": [
                            descriptor.id + "." + r for r in descriptor.relations
                        ],
                        "scenario_version": descriptor.version,
                    }
                    sha = write_output(layout, tenant, kb, result)
                    layout.atomic_write(cache, json.dumps({"revision": sha}).encode())
                await check()
                sha = write_output(layout, tenant, kb, result)
                async with layout.version_lock(tenant, kb):
                    await check()
                    state = read_state(layout, tenant, kb)
                    state["scenarios"][scenario_id].update(
                        status="ready",
                        result_revision=sha,
                        coverage=result["coverage"],
                        finished_at=now(),
                    )
                    layout.set_version_pin(
                        tenant, kb, "scenario-output:" + sha, view.manifest_hash
                    )
                    write_state(layout, tenant, kb, state)
                await record("completed", revision=sha, coverage=result["coverage"])
        except asyncio.CancelledError:
            event(layout, tenant, kb, config["job_id"], "cancelled")
            async with layout.version_lock(tenant, kb):
                state = read_state(layout, tenant, kb)
                current = state["scenarios"].get(scenario_id, {})
                if current.get("job_id") == config["job_id"] and current.get("enabled"):
                    current["status"] = "stale"
                    write_state(layout, tenant, kb, state)
        except Exception as exc:
            event(
                layout,
                tenant,
                kb,
                config["job_id"],
                "failed",
                error=type(exc).__name__,
                message=str(exc)[:3000],
            )
            async with layout.version_lock(tenant, kb):
                state = read_state(layout, tenant, kb)
                current = state["scenarios"].get(scenario_id, {})
                if current.get("job_id") == config["job_id"] and current.get("enabled"):
                    current.update(status="failed", error=str(exc)[:3000])
                    write_state(layout, tenant, kb, state)

    async def operation(self, tenant, kb, scenario_id, operation, options, actor=""):
        if not isinstance(options, dict):
            raise ValidationError("SCENARIO_OPTIONS_INVALID")
        descriptor = self.registry.get(scenario_id)
        if operation not in descriptor.operations:
            raise ValidationError("SCENARIO_OPERATION_UNKNOWN")
        async with self.app.layout.version_lock(tenant, kb):
            state = read_state(self.app.layout, tenant, kb)
            config = state["scenarios"].get(scenario_id, {})
            if not config.get("enabled"):
                raise ConflictError("SCENARIO_DISABLED")
            if config.get("scenario_version") != descriptor.version:
                raise ConflictError("SCENARIO_VERSION_CHANGED")
            if config.get("implementation_identity") != self.implementation_identity(
                descriptor
            ):
                raise ConflictError("SCENARIO_IMPLEMENTATION_CHANGED")
            if self.view(tenant, kb).manifest_hash != config.get("base_revision"):
                raise ConflictError("SCENARIO_BASE_CHANGED")
            if operation in {"materials", "material", "annotate"}:
                from .materials import collect, unit_summary, public_unit

                view = self.view(tenant, kb, config["base_revision"])
                units = await asyncio.to_thread(
                    collect, view, self.app.layout, config["document_ids"]
                )
                if operation == "materials":
                    if set(options) - {"offset", "limit", "document_id"}:
                        raise ValidationError("SCENARIO_OPTIONS_INVALID")
                    offset, limit = options.get("offset", 0), options.get("limit", 50)
                    if (
                        type(offset) is not int
                        or offset < 0
                        or type(limit) is not int
                        or not 1 <= limit <= 200
                    ):
                        raise ValidationError("SCENARIO_PAGE_INVALID")
                    selected = [
                        u
                        for u in units
                        if not options.get("document_id")
                        or u["document_id"] == options["document_id"]
                    ]
                    return {
                        "items": [
                            unit_summary(u) for u in selected[offset : offset + limit]
                        ],
                        "total": len(selected),
                        "has_more": offset + limit < len(selected),
                    }
                if operation == "material":
                    if set(options) != {"unit_id"}:
                        raise ValidationError("SCENARIO_OPTIONS_INVALID")
                    unit = next(
                        (u for u in units if u["unit_id"] == options["unit_id"]), None
                    )
                    if unit is None:
                        raise NotFoundError("SCENARIO_UNIT_OUT_OF_SCOPE")
                    return {
                        "unit": public_unit(unit),
                        "base_revision": view.manifest_hash,
                    }
                from .semantic import Item, ground_fragments
                from pydantic import Field

                class Annotation(Item):
                    local_id: str = "manual"
                    expected_version: int = Field(ge=0, strict=True)

                if not actor:
                    raise ValidationError("SCENARIO_ACTOR_REQUIRED")
                if config.get("status") in {"running", "queued"}:
                    raise ConflictError("SCENARIO_JOB_RUNNING")
                try:
                    annotation = Annotation.model_validate(options)
                except ValueError as exc:
                    raise ValidationError(
                        "SCENARIO_ANNOTATION_INVALID: " + str(exc)
                    ) from exc
                if annotation.expected_version != state["version"]:
                    raise ConflictError("SCENARIO_CONFIG_VERSION_CHANGED")
                if annotation.kind not in descriptor.kinds:
                    raise ValidationError("SCENARIO_KIND_UNREGISTERED")
                refs = ground_fragments(view, units, annotation.fragments)
                oid = (
                    descriptor.id
                    + "."
                    + annotation.kind
                    + "."
                    + digest([refs, annotation.label, annotation.interpretation])[:24]
                )
                previous = config.get("result_revision")
                result = (
                    read_output(self.app.layout, tenant, kb, previous)
                    if previous
                    else {
                        "schema": "wiki.scenario-resources.v1",
                        "base_revision": view.manifest_hash,
                        "scenario_id": scenario_id,
                        "scenario_version": descriptor.version,
                        "objects": [],
                        "relations": [],
                        "calls": [],
                        "units": [unit_summary(u) for u in units],
                        "coverage": {
                            "units": {
                                u["unit_id"]: {
                                    "status": "unprocessed",
                                    "reason": "automatic_interpretation_not_run",
                                }
                                for u in units
                            },
                            "processed": 0,
                            "total": len(units),
                            "processing_complete": False,
                            "semantic_completeness": "not_certified",
                        },
                    }
                )
                result["objects"] = [
                    o for o in result["objects"] if o["object_id"] != oid
                ]
                result["objects"].append(
                    {
                        "object_id": oid,
                        "kind": scenario_id + "." + annotation.kind,
                        "label": annotation.label,
                        "interpretation": annotation.interpretation,
                        "missing": annotation.missing,
                        "uncertainties": annotation.uncertainties,
                        "fragments": refs,
                        "document_ids": sorted({r["document_id"] for r in refs}),
                        "review_status": "candidate",
                        "origin": {
                            "kind": "human_annotation",
                            "actor": actor,
                            "at": now(),
                        },
                    }
                )
                result["parent_revision"] = previous
                for ref in refs:
                    result["coverage"]["units"].setdefault(
                        ref["unit_id"], {"status": "unprocessed"}
                    )["manually_marked"] = True
                result["coverage"]["manually_marked_units"] = sum(
                    bool(u.get("manually_marked"))
                    for u in result["coverage"]["units"].values()
                )
                result["configuration_identity"] = digest(["human", config, refs])
                revision = write_output(self.app.layout, tenant, kb, result)
                if previous:
                    old = (
                        scope_root(self.app.layout, tenant, kb)
                        / "reviews"
                        / (previous + ".json")
                    )
                    if old.is_file():
                        self.app.layout.atomic_write(
                            old.with_name(revision + ".json"), old.read_bytes()
                        )
                self.app.layout.set_version_pin(
                    tenant, kb, "scenario-output:" + revision, view.manifest_hash
                )
                config.update(
                    result_revision=revision,
                    status="ready",
                    coverage=result["coverage"],
                )
                state["version"] += 1
                write_state(self.app.layout, tenant, kb, state)
                return {
                    "object_id": oid,
                    "revision": revision,
                    "version": state["version"],
                }
            if operation == "interpret":
                if not actor:
                    raise ValidationError("SCENARIO_ACTOR_REQUIRED")
                if set(options) - {"unit_ids"}:
                    raise ValidationError("SCENARIO_OPTIONS_INVALID")
                ids = options.get("unit_ids")
                if ids is not None and (
                    not isinstance(ids, list)
                    or not ids
                    or len(ids) > 100
                    or any(not isinstance(v, str) for v in ids)
                ):
                    raise ValidationError("SCENARIO_UNITS_INVALID")
                if config.get("status") in {"running", "queued"}:
                    raise ConflictError("SCENARIO_JOB_RUNNING")
                config = dict(config)
                config["job_id"] = uuid.uuid4().hex
                config["status"] = "queued"
                state["version"] += 1
                config["config_version"] = state["version"]
                state["scenarios"][scenario_id] = config
                write_state(self.app.layout, tenant, kb, state)
                self.schedule(tenant, kb, scenario_id, config, ids)
                return {
                    "job_id": config["job_id"],
                    "status": "queued",
                    "version": state["version"],
                }
            if config.get("status") != "ready" or not config.get("result_revision"):
                raise ConflictError("SCENARIO_RESULTS_NOT_READY")
            result = read_output(self.app.layout, tenant, kb, config["result_revision"])
            review_path = (
                scope_root(self.app.layout, tenant, kb)
                / "reviews"
                / (config["result_revision"] + ".json")
            )
            review = (
                json.loads(review_path.read_text())
                if review_path.is_file()
                else {"version": 0, "objects": {}, "events": []}
            )
            if operation == "selection":
                from .contracts import SelectionRequest, ScenarioSelection

                if not actor:
                    raise ValidationError("SCENARIO_ACTOR_REQUIRED")
                try:
                    request = SelectionRequest.model_validate(options)
                except ValueError as exc:
                    raise ValidationError(
                        "SCENARIO_SELECTION_INVALID: " + str(exc)
                    ) from exc
                if request.expected_revision != config["result_revision"]:
                    raise ConflictError("SCENARIO_RESULT_CHANGED")
                if request.expected_review_version != review["version"]:
                    raise ConflictError("SCENARIO_REVIEW_VERSION_CHANGED")
                lookup = {o["object_id"]: o for o in result["objects"]}
                selected = []
                for oid in dict.fromkeys(request.object_ids):
                    if oid not in lookup:
                        raise NotFoundError("SCENARIO_OBJECT_NOT_FOUND")
                    if (
                        review["objects"].get(oid, {}).get("review_status")
                        != "accepted"
                    ):
                        raise ConflictError("SCENARIO_SELECTION_NEEDS_REVIEW")
                    selected.append({**lookup[oid], "review_status": "accepted"})
                snapshot = ScenarioSelection(
                    tenant_id=tenant,
                    kb_id=kb,
                    base_revision=result["base_revision"],
                    scenario_id=scenario_id,
                    scenario_version=result["scenario_version"],
                    result_revision=config["result_revision"],
                    review_version=review["version"],
                    objects=selected,
                    actor=actor,
                    created_at=now(),
                ).model_dump()
                sha = digest(snapshot)
                self.app.layout.atomic_write(
                    scope_root(self.app.layout, tenant, kb)
                    / "selections"
                    / (sha + ".json"),
                    json.dumps(
                        snapshot,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode(),
                )
                self.app.layout.set_version_pin(
                    tenant, kb, "scenario-selection:" + sha, result["base_revision"]
                )
                return {"selection_id": sha, **snapshot}
            if operation == "review":
                if not actor:
                    raise ValidationError("SCENARIO_ACTOR_REQUIRED")
                if set(options) - {"expected_version", "object_id", "status", "note"}:
                    raise ValidationError("SCENARIO_REVIEW_INVALID")
                if (
                    type(options.get("expected_version")) is not int
                    or options["expected_version"] != review["version"]
                ):
                    raise ConflictError("SCENARIO_REVIEW_VERSION_CHANGED")
                oid = options.get("object_id")
                if oid not in {o["object_id"] for o in result["objects"]}:
                    raise NotFoundError("SCENARIO_OBJECT_NOT_FOUND")
                if options.get("status") not in {"accepted", "rejected", "candidate"}:
                    raise ValidationError("SCENARIO_REVIEW_INVALID")
                note = str(options.get("note") or "")
                if len(note) > 4000:
                    raise ValidationError("SCENARIO_REVIEW_NOTE_LIMIT")
                review["version"] += 1
                review["objects"][oid] = {"review_status": options["status"]}
                review["events"].append(
                    {
                        "actor": actor,
                        "at": now(),
                        "object_id": oid,
                        "status": options["status"],
                        "note": note,
                    }
                )
                self.app.layout.atomic_write(
                    review_path,
                    json.dumps(review, ensure_ascii=False, indent=2).encode(),
                )
                return {"review_version": review["version"], "object_id": oid}
            if set(options) - {
                "kind",
                "query",
                "document_id",
                "reviewed_only",
                "offset",
                "limit",
                "revision",
                "review_version",
            }:
                raise ValidationError("SCENARIO_OPTIONS_INVALID")
            if (
                options.get("revision")
                and options["revision"] != config["result_revision"]
            ):
                raise ConflictError("SCENARIO_RESULT_CHANGED")
            if (
                options.get("review_version") is not None
                and options["review_version"] != review["version"]
            ):
                raise ConflictError("SCENARIO_REVIEW_VERSION_CHANGED")
            offset, limit = options.get("offset", 0), options.get("limit", 50)
            if (
                type(offset) is not int
                or offset < 0
                or type(limit) is not int
                or not 1 <= limit <= 200
            ):
                raise ValidationError("SCENARIO_PAGE_INVALID")
            objects = [
                {**o, **review["objects"].get(o["object_id"], {})}
                for o in result["objects"]
            ]
            for key in ("kind", "document_id"):
                if options.get(key):
                    objects = [
                        o
                        for o in objects
                        if (
                            o["kind"] == options[key]
                            if key == "kind"
                            else options[key] in o["document_ids"]
                        )
                    ]
            if (
                "reviewed_only" in options
                and type(options["reviewed_only"]) is not bool
            ):
                raise ValidationError("SCENARIO_OPTIONS_INVALID")
            if options.get("reviewed_only"):
                objects = [o for o in objects if o["review_status"] == "accepted"]
            if options.get("query"):
                q = str(options["query"]).casefold()
                objects = [
                    o
                    for o in objects
                    if q in json.dumps(o, ensure_ascii=False).casefold()
                ]
            return {
                "base_revision": result["base_revision"],
                "revision": config["result_revision"],
                "review_version": review["version"],
                "coverage": result["coverage"],
                "items": objects[offset : offset + limit],
                "rejected": result.get("rejected", []) if offset == 0 else [],
                "total": len(objects),
                "has_more": offset + limit < len(objects),
                "relations": result["relations"],
                "units": result["units"] if offset == 0 else [],
            }

    async def stop(self):
        tasks = list(self.jobs.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def read_selection(self, tenant, kb, selection_id):
        """The host must authorize historical KB reads. Does not enable a scene."""
        if not re.fullmatch(r"[0-9a-f]{64}", selection_id or ""):
            raise ValidationError("SCENARIO_SELECTION_ID_INVALID")
        path = (
            scope_root(self.app.layout, tenant, kb)
            / "selections"
            / (selection_id + ".json")
        )
        if not path.is_file():
            raise NotFoundError("SCENARIO_SELECTION_NOT_FOUND")
        snapshot = json.loads(path.read_text())
        if digest(snapshot) != selection_id or (
            snapshot["tenant_id"],
            snapshot["kb_id"],
        ) != (tenant, kb):
            raise ValidationError("SCENARIO_SELECTION_INTEGRITY_INVALID")
        return {"selection_id": selection_id, **snapshot}
