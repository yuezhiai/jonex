"""LLM-Wiki V1 compilation engine.

Orchestrates source digestion, incremental Algorithm 1 compilation, structured
LLM output validation, and an immutable portable Wiki bundle production.

All model calls go through the injected ModelPort (never direct LLM provider
access), and all storage through the injected ArtifactStore.  The engine
delegates the whole Algorithm 1 / Error Book pipeline to the production
composition module; it never imports or calls the legacy page-operation model.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, List, Mapping

from jonex_wiki.kernel.contracts import (
    EffectiveRuntimeSnapshot,
    EngineDescriptor,
    SourceInventorySnapshot,
    default_models_from_env,
)
from jonex_wiki.kernel.ports import (
    ArtifactStore,
    CancelPort,
    ModelPort,
    ProgressSink,
    SourceArtifactPort,
)
from jonex_wiki.kernel.runtime_env import current_env
from jonex_wiki.kernel.telemetry import (RuntimeTelemetryRecorder, telemetry_stage, timed_async_stage)

from . import bundle

logger = logging.getLogger(__name__)
from jonex_wiki.domain.engine.compile.digest import (source_fingerprint)
from . import bundle_codec
from . import pipeline
from jonex_wiki.domain.engine.compile.error_book import ErrorBook
from jonex_wiki.domain.engine.query.algorithm import (
    TOOL_CHECKSUM,
    QueryRunResult,
    build_query_corpus,
)


def _article_work_pending(view, curation):
    """A source-unchanged Bundle may still need the direct-file writer."""
    return bool(curation.get('agent_article_rule')) and (
        view is None or 'articles/authored/index.md' not in view.list_entries()
    )


# eab1e58 left sealed private candidates when bounded article turns failed.
# The execution-policy upgrade keeps the prepared material format and rules.
_LEGACY_ARTICLE_SOURCE_CODE = {
    "source_tree_sha256": "aafdc5b7f8d7c857d48ed7807bb2e71dc35f3037c961ccb6bb2b590fbf1733e2",
    "compiler_sha256": "fc3a12d05f1c4da0c06bda6853056262f31d77398a4f3d70e3b7220afe268cb2",
}


# 来源缓存的身份排除只影响调度的参数。整包复用另检查发布内容和路由。
_RUNTIME_ONLY_CURATION_KEYS = frozenset({
    "compile_concurrency",
    "compile_scheduler",
    "max_passage_failure_rate",
    "repair_interval",
    "publication_only_migration",
})


def _record_contribution_rejections(book: ErrorBook, rejections, state) -> ErrorBook:
    """One Error Book row per refused contribution, never a silent drop.

    A record whose source was replaced while the compile was in flight is a
    real loss of a fact the previous Bundle published; recording it makes the
    withdrawal auditable instead of leaving a fact that quietly disappears.
    """
    if not rejections:
        return book
    from jonex_wiki.domain.engine.compile.error_book import ErrorBookEntry
    from jonex_wiki.domain.engine.compile.publication import RULE_VERSION, digest

    entries = list(book.entries)
    known = {entry.id for entry in entries}
    for rejection in rejections:
        source_id = str(rejection.get("source_id", ""))
        code = str(rejection.get("code", "SOURCE_CONTRIBUTION_REJECTED"))
        issue_id = "error_" + digest([RULE_VERSION, code, source_id,
                                      str(rejection.get("detail", ""))])[:24]
        if issue_id in known:
            continue
        entries.append(ErrorBookEntry(
            id=issue_id, category="coverage_gap", code=code,
            phenomenon=str(rejection.get("detail", ""))[:1500],
            error_class="Advisory",
            root_cause="The source version this compile read no longer stands.",
            constraint="Publish only contributions whose current source version matches.",
            verification="Recompile the affected source and revalidate its contributions.",
            affected_source_ids=[source_id] if source_id else [],
            rule_version=RULE_VERSION,
            source_versions={source_id: str(state.source_archive[pid].sha256)
                             for pid in state.source_archive
                             if state.source_archive[pid].source_id == source_id}
            if source_id else {},
            first_seen_batch=max(1, state.batch_number),
            last_seen_batch=max(1, state.batch_number)))
        known.add(issue_id)
    return ErrorBook(entries=entries)


def _prune_dangling_displays(contribution_set, claims):
    """Drop a display that points at a claim this publication does not carry.

    A ledger whose display names a claim no reader can open is worse than a
    ledger without the display: the fact stays, the page binding goes, and the
    inconsistency is reported rather than published.
    """
    live = {(claim.page_path, claim.claim_id) for claim in claims}
    dropped: List[Dict[str, str]] = []
    records = []
    for record in contribution_set.records:
        keep = [display for display in record.displays
                if (display.page_path, display.claim_id) in live]
        if len(keep) != len(record.displays):
            for display in record.displays:
                if (display.page_path, display.claim_id) not in live:
                    dropped.append({
                        "source_id": (record.source_spans[0].source_id
                                      if record.source_spans else ""),
                        "code": "SOURCE_CONTRIBUTION_DANGLING_DISPLAY",
                        "detail": "contribution {} is no longer shown on {} ({})".format(
                            record.record_id, display.page_path, display.claim_id),
                    })
        records.append(record.model_copy(update={"displays": keep}))
    if not dropped:
        return contribution_set, dropped
    from jonex_wiki.domain.engine.compile import source_contribution

    pruned = source_contribution.SourceContributionSet(
        records=records,
        by_source=contribution_set.by_source,
        fact_index=contribution_set.fact_index,
        source_versions=contribution_set.source_versions,
        rebuilt_from_legacy=contribution_set.rebuilt_from_legacy,
    )
    by_source, fact_index = source_contribution._index(records)
    return pruned.model_copy(update={"by_source": by_source, "fact_index": fact_index}), dropped


# [jonex] 查询/编译的模型名以**当下 env** 为权威；Manifest 里的 models 只是编译期快照。
# 线上事故(2026-10-08)：env 已改成新模型，KB 快照仍钉着旧名字 → 上游 402，
# 且日志里显示的正是旧名字，看不出是"没读到新配置"。
def _resolve_query_model(manifest_query_model: str | None) -> str | None:
    """查询用的模型名：**当前环境变量优先**，Manifest 记录值仅作回落。

    ``Manifest["models"]["query"]`` 是**编译期快照**（保留下来供审计/溯源），但它
    不是查询期的权威：换模型之后旧 KB 若继续按快照发请求，就会用一个部署里早已
    不存在的名字 —— 2026-10-08 现场：容器 env 已是新模型名（``printenv``
    两个变量都对），KB 快照仍钉着**先前那个已欠费的旧名字** → 上游 402，而且
    日志里显示的正是那个旧名字，**看不出是「没读到新配置」**。

    读 ``current_env()`` 而非 ``os.environ``：查询链路把执行包在
    ``runtime_environment(settings.runtime_env())`` 里（见 ``kernel/runtime_env.py``），
    注入生效时只有前者读得到。env 与 Manifest 都没有时返回 ``None``（沿用原行为）。
    """
    env_model = (current_env().get("LLM_MODEL", "") or "").strip()
    return env_model or manifest_query_model


def _compilation_manifest(manifest: dict) -> dict:
    """Only settings that can change compiled claims/pages or published indexes.

    Query routing is still stored in the new Bundle manifest, but changing it
    need not replay unchanged source passages through the model.
    """
    relevant = {key: value for key, value in manifest.items()
                if key not in {"checksum", "query"}}
    relevant["models"] = {key: value for key, value in (manifest.get("models") or {}).items()
                          if key != "query"}
    relevant["curation"] = {
        key: value for key, value in (manifest.get("curation") or {}).items()
        if key not in _RUNTIME_ONLY_CURATION_KEYS
    }
    return relevant


def _publication_manifest_matches(previous, current: dict) -> bool:
    """Reuse a completed publication across a change in scheduling width."""
    if previous == current:
        return True
    if not isinstance(previous, dict):
        return False
    before = previous.get("curation") or {}
    after = current.get("curation") or {}
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    # ManifestBuilder's checksum includes concurrency, so compare the actual
    # settings when that is the only changed field.
    return (
        before.get("compile_concurrency") != after.get("compile_concurrency")
        and {k: v for k, v in before.items() if k != "compile_concurrency"}
        == {k: v for k, v in after.items() if k != "compile_concurrency"}
        and {k: v for k, v in previous.items() if k not in {"curation", "checksum"}}
        == {k: v for k, v in current.items() if k not in {"curation", "checksum"}}
    )


def _compilation_runtime_matches(old, current) -> bool:
    """Compare the effective compiler runtime inputs."""
    return (
        ((old.code.compiler_sha256 == current.code.compiler_sha256)
         if old.code.compiler_sha256 and current.code.compiler_sha256
         else old.code.source_tree_sha256 == current.code.source_tree_sha256)
        and old.prompt_checksum == current.prompt_checksum
        and old.environment_overrides == current.environment_overrides
        and {key: value for key, value in old.models.items() if key not in {"query", "embedding"}}
        == {key: value for key, value in current.models.items() if key not in {"query", "embedding"}}
    )


def build_table_registry(directory_view: Any) -> dict[str, dict[str, str]]:
    """Map every published table id to its owner and grid inside one Revision.

    The source digest, ``sources/registry.json`` and the ``tables.sqlite`` grid
    must agree; one table id may never resolve to two owners or two grids.
    """
    digest_by_slug: dict[str, dict] = {}
    digest_by_source: dict[str, dict] = {}
    for path in directory_view.list_entries("sources/digests"):
        payload = json.loads(directory_view.read_text(path))
        digest_by_slug[path.rsplit("/", 1)[-1].rsplit(".", 1)[0]] = payload
        digest_by_source[str(payload.get("source_id") or "")] = payload
    entries = directory_view.list_entries("sources")
    registry_by_source: dict[str, dict] = {}
    if "sources/registry.json" in entries:
        registry = json.loads(directory_view.read_text("sources/registry.json"))
        for entry in registry.get("sources", ()):
            registry_by_source[str(entry.get("source_id"))] = entry
    for source_id, entry in sorted(registry_by_source.items()):
        digest = digest_by_source.get(source_id)
        if digest is None:
            raise ValueError("SOURCE_REGISTRY_WITHOUT_DIGEST: " + source_id)
        if entry.get("document_id") != digest.get("document_id"):
            raise ValueError("SOURCE_REGISTRY_DOCUMENT_MISMATCH: " + source_id)
    table_registry: dict[str, dict[str, str]] = {}
    for path in entries:
        if not path.endswith("/tables.sqlite"):
            continue
        owner = digest_by_slug.get(path.split("/")[1])
        if owner is None:
            raise ValueError("TABLE_ARTIFACT_WITHOUT_SOURCE_DIGEST: " + path)
        source_id = owner.get("source_id")
        document_id = owner.get("document_id")
        if not isinstance(source_id, str) or not source_id or not isinstance(document_id, str) or not document_id:
            raise ValueError("TABLE_ARTIFACT_OWNER_INVALID: " + path)
        entry = registry_by_source.get(source_id)
        if entry is not None and entry.get("tables") not in (None, path):
            raise ValueError("SOURCE_REGISTRY_TABLE_PATH_MISMATCH: " + source_id)
        connection = directory_view.open_sqlite(path)
        try:
            table_ids = sorted(str(row[0]) for row in connection.execute("SELECT table_id FROM logical_tables"))
        finally:
            connection.close()
        declared = entry.get("table_ids") if entry is not None else None
        if isinstance(declared, list) and sorted(str(value) for value in declared) != table_ids:
            raise ValueError("SOURCE_REGISTRY_TABLE_IDS_MISMATCH: " + source_id)
        if entry is not None and entry.get("tables") and not table_ids:
            raise ValueError("TABLE_ARTIFACT_WITHOUT_TABLES: " + source_id)
        for table_id in table_ids:
            info = {"path": path, "source_id": source_id, "document_id": document_id}
            existing = table_registry.get(table_id)
            if existing is not None and existing != info:
                raise ValueError("TABLE_REGISTRY_OWNER_CONFLICT: " + table_id)
            table_registry[table_id] = info
    return table_registry


@dataclass
class RevisionArtifacts:
    """In-memory state of the published revision after a compilation run."""

    revision_id: str
    pages: dict[str, dict]  # path → serialized WikiPageSnapshot
    claims: list[dict]
    graph: list[dict]
    ledger: list[dict]
    errors: list[dict]
    checksums: dict[str, str]
    digest_map: dict[str, str]  # source_id → fingerprint
    source_inventory_digest: str = ""
    runtime_snapshot_checksum: str = ""
    source_count: int = 0
    page_count: int = 0
    claim_count: int = 0
    error_count: int = 0

    def add_page(self, path: str, page: dict) -> None:
        self.pages[path] = page
        self.page_count = len(self.pages)

    def add_claim(self, claim: dict) -> None:
        self.claims.append(claim)
        self.claim_count = len(self.claims)

    def add_error(self, error: dict) -> None:
        self.errors.append(error)
        self.error_count = len(self.errors)


class LLMWikiV1Engine:
    """Concrete implementation of the LLM-Wiki compilation algorithm.

    Registered as ``llm_wiki`` in the ContextEngine registry.
    All external dependencies are injected through ports.
    """

    ENGINE_ID = "llm_wiki"
    ENGINE_VERSION = "1.0.0"
    SCHEMA_VERSION = 1

    @staticmethod
    def describe() -> EngineDescriptor:
        return EngineDescriptor(
            engine_id="llm_wiki",
            engine_version="1.0.0",
            schema_version=1,
            models=default_models_from_env(),
            curation_config={
                "pageSelectionLimit": 5,
                "llmReviewEveryBatches": 10,
                "finalRepairRounds": 3,
                "allowPartialSources": False,
            },
            query_config={"strategy": "agent_direct"},
            schema={
                "type": "object",
                "properties": {
                    "pageSelectionLimit": {
                        "type": "integer", "minimum": 1, "maximum": 20, "default": 5,
                    },
                },
            },
        )

    @classmethod
    def freeze_source_inventory(
        cls, artifacts: list[Any], manifest: dict
    ) -> tuple[dict[str, str], SourceInventorySnapshot]:
        """Return compiler fingerprints and the canonical Source snapshot."""
        manifest_checksum = bundle.sha256_hex(
            pipeline.canonical_json(_compilation_manifest(manifest)).encode("utf-8"))
        by_source: Dict[str, List[Any]] = {}
        for artifact in artifacts:
            by_source.setdefault(artifact.source_id, []).append(artifact)
        digests = {
            source_id: source_fingerprint(
                source_id,
                source_artifacts,
                prompt_checksum=pipeline.PROMPT_CHECKSUM,
                manifest_checksum=manifest_checksum,
                engine_id=cls.ENGINE_ID,
                engine_version=cls.ENGINE_VERSION,
            )
            for source_id, source_artifacts in by_source.items()
        }
        return digests, bundle.build_source_inventory(artifacts, digests)

    @timed_async_stage('compile_total')
    async def compile(
        self,
        context_id: str,
        tenant_id: str,
        manifest: dict,
        *,
        sources: SourceArtifactPort,
        model: ModelPort,
        store: ArtifactStore,
        progress: ProgressSink | None = None,
        previous_digest_map: dict[str, str] | None = None,
        previous_bundle: bytes | str | None = None,
        previous_bundle_key: str | None = None,
        publication_parent_revision: str | None = None,
        artifact_attempt_id: str | None = None,
        telemetry: RuntimeTelemetryRecorder | None = None,
        cancel: CancelPort | None = None,
        compile_main_prompt: str = "",
        article_preference: str = "",
        omission_confirmations: "Sequence[Mapping[str, Any]]" = (),
        agent_compiler=None,
    ) -> tuple[RevisionArtifacts, str]:
        """Compile source records and, when configured, agent-authored articles.

        A fully unchanged publication may be reused. Source changes can require
        article writing again even when unchanged source records are reused.
        """
        # Resolve the mode before fingerprints and previous-Bundle reuse.  New
        # calls always carry an explicit mode, including hand-written manifests
        # that omit it; historical manifests remain untouched when decoded.
        # Otherwise a missing field could replay an old legacy publication.
        curation = dict(manifest.get("curation") or {})
        if curation.get("compile_mode", "layered") != "layered":
            raise ValueError("only layered compilation is supported")
        curation["compile_mode"] = "layered"
        curation["publication_layout"] = "agent-files-v2"
        # Organization changes must create a new publication even when source
        # understanding can be reused. Historical Bundles cold-open unchanged.
        from jonex_wiki.domain.engine.compile.rendering import READING_RENDER_VERSION
        from jonex_wiki.domain.engine.compile.source_relations import RULE as SOURCE_RELATION_RULE
        from jonex_wiki.domain.engine.compile.organization import ALIAS_EVIDENCE_RULE, ORGANIZATION_RULE_VERSION
        from jonex_wiki.domain.engine.compile.relation_reading_projection import RULE as CLAIM_READING_RULE
        from jonex_wiki.domain.engine.compile.relation_increment import RELATION_RULE_VERSION as PAGE_RELATION_RULE
        curation['reading_organization_rule']=READING_RENDER_VERSION
        curation['source_relation_rule']=SOURCE_RELATION_RULE
        curation['alias_evidence_rule']=ALIAS_EVIDENCE_RULE
        curation['source_organization_rule']=ORGANIZATION_RULE_VERSION
        curation['claim_reading_rule']=CLAIM_READING_RULE
        curation['page_relation_rule']=PAGE_RELATION_RULE
        from jonex_wiki.domain.engine.compile.coherent_catalog import RULE as COHERENT_RULE, CONTEXT_RULE
        from jonex_wiki.domain.engine.compile.source_heading_references import RULE as HEADING_REFERENCE_RULE
        curation['coherent_reading_rule']=COHERENT_RULE
        curation['coherent_context_rule']=CONTEXT_RULE
        curation['source_heading_reference_rule']=HEADING_REFERENCE_RULE
        # The production task supplies the direct-file writer. Remove the old
        # material-tool writer switch so it cannot silently start a second route.
        curation.pop('coherent_article_compilation', None)
        if agent_compiler is not None:
            curation['agent_article_rule'] = agent_compiler.RULE
            curation['agent_article_skill'] = getattr(agent_compiler, 'skill_sha', '')
            curation['agent_article_implementation'] = getattr(agent_compiler, 'implementation_sha', '')
            if article_preference:
                import hashlib
                curation['agent_article_preference_sha256'] = hashlib.sha256(article_preference.encode()).hexdigest()
        manifest = {**manifest, "curation": curation}

        if cancel:
            cancel.raise_if_cancelled()
        if progress:
            await progress.update("", "sources", 5, "Fetching source artifacts")

        with telemetry_stage(telemetry, "source_read"):
            all_artifacts = await sources.get_artifacts(context_id, tenant_id)

        if any(a.source_metadata.get('original_structure') for a in all_artifacts):
            from .source_identity import RULE as IDENTITY_RULE
            curation['source_identity_rule']=IDENTITY_RULE
            manifest={**manifest,'curation':curation}

        manifest_checksum = manifest.get("checksum", "")

        # One deterministic digest per logical source id over every ordered
        # artifact for that source; a multi-chunk source never collapses to one
        # chunk and caller order cannot alter the inventory identity.
        with telemetry_stage(telemetry, "source_inventory"):
            curr_digests, source_inventory = self.freeze_source_inventory(
                all_artifacts, manifest
            )
            effective_runtime = bundle.capture_effective_runtime(
                manifest,
                prompt_checksum=pipeline.PROMPT_CHECKSUM,
                tool_checksum=TOOL_CHECKSUM,
            )
            runtime_snapshot_checksum = bundle.snapshot_checksum(
                effective_runtime
            )
        def revision_for(config, digests, inventory, runtime_checksum):
            return pipeline.derive_revision_id(
                digests,
                manifest_checksum=manifest_checksum,
                manifest_payload_checksum=bundle.sha256_hex(
                    pipeline.canonical_json(config).encode("utf-8")
                ),
                context_id=context_id,
                tenant_id=tenant_id,
                prompt_checksum=pipeline.PROMPT_CHECKSUM,
                engine_id=self.ENGINE_ID,
                engine_version=self.ENGINE_VERSION,
                source_inventory_digest=inventory.digest,
                runtime_snapshot_checksum=runtime_checksum,
                # LWP-CURATE-RECOVERY (P1-1): scope the revision id to the
                # exact bundle codec version so a schema upgrade can never
                # re-derive a legacy revision's id (primary-key collision).
                bundle_schema_version=bundle.SCHEMA_VERSION,
            )

        revision_id = revision_for(manifest, curr_digests, source_inventory, runtime_snapshot_checksum)

        if not all_artifacts:
            return await self._empty_compile(
                context_id,
                tenant_id,
                revision_id,
                manifest,
                store,
                progress,
                source_inventory=source_inventory,
                effective_runtime=effective_runtime,
                artifact_attempt_id=artifact_attempt_id,
                telemetry=telemetry,
            )

        fingerprints = {"source_inventory": source_inventory.digest,
                        "runtime": runtime_snapshot_checksum, "query_policy": "wiki-agent-v1"}
        article_source_key = getattr(store, "article_source_key", None)
        resume_parent = publication_parent_revision
        if article_source_key and agent_compiler is not None and resume_parent is None:
            resume_parent = await self._published_revision_id(store, tenant_id, context_id)
        def source_identity(revision):
            return bundle_codec.canonical_checksum([revision, resume_parent, compile_main_prompt])

        article_source_identity = source_identity(revision_id) if (
            article_source_key and agent_compiler is not None and not omission_confirmations) else ""

        async def finish_articles(candidate, view, counts):
            from hashlib import sha256
            from jonex_wiki.domain.engine.compile.coherent_catalog import digest
            from jonex_wiki.domain.reading.authored_articles import check_authored_table_links

            if progress:
                await progress.update("", "authoring", 88, "Writing source-grounded articles")
            with telemetry_stage(telemetry, "agent_article_compilation"):
                summary = await agent_compiler.compile(
                    view=view, model=(manifest.get("models") or {}).get("curation") or "",
                    tenant_id=tenant_id, context_id=context_id,
                    run_id=artifact_attempt_id or view.revision_id, cancel=cancel,
                    **({"article_preference": article_preference} if article_preference else {}),
                )
            if cancel:
                cancel.raise_if_cancelled()
            if progress:
                await progress.update("", "validate", 95, "Validating authored articles")
            summary["files"], issues = check_authored_table_links(view, summary["files"])
            if issues:
                summary["table_link_issues"] = issues
            files_digest = [(path, sha256(data).hexdigest()) for path, data in sorted(summary["files"].items())]
            authored_revision = digest([view.revision_id, agent_compiler.RULE, files_digest])
            counts["synthesized_articles"] = sum(
                path.rsplit("/", 1)[-1] != "index.md" for path, _ in files_digest)
            counts["article_model_calls"] = summary.get("gateway_calls", summary["model_calls"])
            with telemetry_stage(telemetry, "bundle_render_and_persist"):
                overlays = await asyncio.to_thread(bundle_codec.authored_publication_overlays,
                                                    view, authored_revision, counts, summary)
                options = {"output_key": artifact_attempt_id or view.revision_id} if (
                    article_source_key and candidate == article_source_key
                ) else {}
                key = await store.finish_authored_revision(candidate, view, authored_revision, overlays,
                                                           fingerprints=fingerprints, **options)
            return authored_revision, key

        if article_source_identity:
            with telemetry_stage(telemetry, "article_source_resume"):
                cached_source = await store.resume_article_source(article_source_identity)
                prior_writer = getattr(agent_compiler, "legacy_checkpoint_implementation", "")
                if cached_source is None and prior_writer:
                    prior_manifest = {**manifest, "curation": {
                        **curation, "agent_article_implementation": prior_writer}}
                    prior_digests, prior_inventory = self.freeze_source_inventory(all_artifacts, prior_manifest)
                    prior_code = effective_runtime.code.model_copy(update=_LEGACY_ARTICLE_SOURCE_CODE)
                    prior_runtime = effective_runtime.model_copy(update={"code": prior_code})
                    prior_checksum = bundle.snapshot_checksum(prior_runtime)
                    prior_revision = revision_for(prior_manifest, prior_digests, prior_inventory, prior_checksum)
                    cached_source = await store.resume_article_source(source_identity(prior_revision))
                    if cached_source is not None:
                        # Preserve the prepared sources' actual provenance. The
                        # new writer fingerprint is stored in compile_agent.json.
                        curr_digests, source_inventory = prior_digests, prior_inventory
                        runtime_snapshot_checksum = prior_checksum
                        fingerprints.update(source_inventory=prior_inventory.digest, runtime=prior_checksum)
            if cached_source is not None:
                from jonex_wiki.kernel.telemetry import count_work
                count_work("article_source_candidate_reused")
                headers = await asyncio.to_thread(lambda: json.loads(cached_source.read_text("manifest.json")))
                counts = headers["counts"]
                revision_id, key = await finish_articles(article_source_key, cached_source, counts)
                head = await self._published_revision_id(store, tenant_id, context_id)
                if head != resume_parent:
                    raise ValueError(f"PUBLICATION_PARENT_CHANGED:{resume_parent}:{head}")
                if progress:
                    await progress.update("", "publish", 100, "Compilation complete")
                return RevisionArtifacts(
                    revision_id=revision_id, pages={}, claims=[], graph=[], ledger=[], errors=[],
                    checksums={}, digest_map=curr_digests, source_inventory_digest=source_inventory.digest,
                    runtime_snapshot_checksum=runtime_snapshot_checksum, source_count=counts["sources"],
                    page_count=counts["pages"], claim_count=counts["claims"], error_count=counts["open_errors"],
                ), key

        routing = {
            "model_name": manifest.get("models", {}).get("curation", ""),
            "tenant_id": tenant_id,
            "context_id": context_id,
            # metering.llm_usage_log.trace_id 是 VARCHAR(64)；compile 的
            # "compile:" + revision_id(64hex) 会超长导致 PG 写入失败（502）。
            "trace_id": ("compile:" + revision_id)[:64],
        }

        # Restore a verified previous bundle when provided.  A corrupt or
        # forged previous bundle is never trusted; it degrades to a full
        # compile rather than silently replaying.  ``previous_digest_map`` is
        # accepted for backward compatibility but cannot restore state without
        # a bundle, so it is intentionally ignored.
        previous = None
        previous_view = None
        # A completed retry candidate can supply reusable compiler work while
        # its publication still belongs on the currently published revision.
        parent_revision = publication_parent_revision
        previous_key = previous_bundle if isinstance(previous_bundle, str) else previous_bundle_key
        if previous_bundle is not None:
            with telemetry_stage(telemetry, "previous_bundle_decode"):
                try:
                    if isinstance(previous_bundle, str):
                        view = await asyncio.to_thread(store.open_revision, previous_bundle)
                        if parent_revision is None:
                            parent_revision = view.revision_id
                        fingerprints = view.manifest.get("fingerprints") or {}
                        published_manifest = await asyncio.to_thread(
                            lambda: json.loads(view.read_text("manifest.json")))
                        if (
                            not omission_confirmations
                            and view.tenant_id == tenant_id
                            and view.context_id == context_id
                            and fingerprints.get("source_inventory") == source_inventory.digest
                            and fingerprints.get("runtime") == runtime_snapshot_checksum
                            and _publication_manifest_matches(published_manifest.get("manifest"), manifest)
                            and not _article_work_pending(view,curation)
                            and not ((published_manifest.get("claim_audit") or {})
                                     .get("summary") or {}).get("failed_passage_count", 0)
                        ):
                            # This early return otherwise skips the ordinary
                            # previous-bundle decoder's checksum validation.
                            verify = getattr(view, "verify_integrity", None)
                            if verify is not None:
                                with telemetry_stage(telemetry, "bundle_integrity"):
                                    await asyncio.to_thread(verify)
                            from jonex_wiki.kernel.telemetry import count_work
                            # The directory fast path must enforce the same
                            # publication boundaries as decoded-bundle reuse.
                            count_work("unchanged_revision_recheck")
                            fresh = await sources.get_artifacts(context_id, tenant_id)
                            fresh_digests, _ = self.freeze_source_inventory(fresh, manifest)
                            if dict(fresh_digests) != curr_digests:
                                count_work("unchanged_revision_reuse_refused")
                                raise ValueError("SOURCE_CONTRIBUTION_STALE_TASK:directory_reuse")
                            head = await self._published_revision_id(store, tenant_id, context_id)
                            if head and head != parent_revision:
                                count_work("unchanged_revision_reuse_refused")
                                raise ValueError("PUBLICATION_PARENT_CHANGED:{}:{}".format(
                                    parent_revision, head))
                            count_work("unchanged_revision_reused")
                            counts = published_manifest.get("counts") or {}
                            return RevisionArtifacts(
                                revision_id=view.revision_id, pages={}, claims=[], graph=[], ledger=[], errors=[],
                                checksums={}, digest_map=dict(curr_digests),
                                source_inventory_digest=source_inventory.digest,
                                runtime_snapshot_checksum=runtime_snapshot_checksum,
                                source_count=int(counts.get("sources", 0)), page_count=int(counts.get("pages", 0)),
                                claim_count=int(counts.get("claims", 0)), error_count=int(counts.get("open_errors", 0)),
                            ), previous_bundle
                        from jonex_wiki.domain.engine.compile.bundle_codec import decode_previous_files, decode_previous_view
                        if hasattr(view, "verify_integrity"):
                            previous = await asyncio.to_thread(decode_previous_view, view)
                            previous_view = view
                        else:
                            previous = await asyncio.to_thread(lambda: decode_previous_files(
                                {path: view.read_bytes(path) for path in view.list_entries()},
                                revision_id=view.revision_id,
                            ))
                    else:
                        try:
                            previous_header = json.loads(previous_bundle.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            previous_header = {}
                        if previous_header.get("format") == "jonex-wiki-directory":
                            from jonex_wiki.domain.engine.compile.bundle_codec import decode_previous_files
                            previous_files = await store.get_revision_files(previous_header)
                            previous = await asyncio.to_thread(decode_previous_files,
                                previous_files, revision_id=previous_header["revision_id"]
                            )
                        else:
                            previous = await asyncio.to_thread(
                                bundle_codec.decode_previous_bundle, previous_bundle)
                    publication_only_migration = bool(
                        (manifest.get("curation") or {}).get(
                            "publication_only_migration", False
                        )
                    )
                    old_manifest = dict(previous.manifest)
                    old_curation = dict(old_manifest.get("curation") or {})
                    old_curation.pop("publication_only_migration", None)
                    for _key in _RUNTIME_ONLY_CURATION_KEYS:
                        old_curation.pop(_key, None)
                    old_manifest["curation"] = old_curation
                    current_manifest = dict(manifest)
                    current_curation = dict(current_manifest.get("curation") or {})
                    current_curation.pop("publication_only_migration", None)
                    for _key in _RUNTIME_ONLY_CURATION_KEYS:
                        current_curation.pop(_key, None)
                    current_manifest["curation"] = current_curation
                    def _source_content_signature(snapshot):
                        return [
                            {k: v for k, v in entry.model_dump().items()
                             if k != "compiler_fingerprint"}
                            for entry in snapshot.entries
                        ]
                    reusable_publication_state = (
                        publication_only_migration
                        and previous.tenant_id == tenant_id
                        and previous.context_id == context_id
                        and _source_content_signature(previous.source_inventory)
                        == _source_content_signature(source_inventory)
                        and old_manifest == current_manifest
                        and previous.effective_runtime.prompt_checksum
                        == pipeline.PROMPT_CHECKSUM
                    )
                    if not reusable_publication_state and (
                        previous.tenant_id != tenant_id
                        or previous.context_id != context_id
                        or _compilation_manifest(previous.manifest) != _compilation_manifest(manifest)
                        or not _compilation_runtime_matches(
                            previous.effective_runtime, effective_runtime)
                    ):
                        from jonex_wiki.kernel.telemetry import count_work
                        count_work("previous_bundle_dependency_mismatch")
                        previous = None
                except ValueError as exc:
                    # A stale publication is not an invalid cache: falling
                    # back must not hide a refused reuse of obsolete input.
                    if str(exc).startswith(("SOURCE_CONTRIBUTION_STALE_TASK:",
                                            "PUBLICATION_PARENT_CHANGED:")):
                        raise
                    from jonex_wiki.kernel.telemetry import count_work
                    count_work("previous_bundle_invalid")
                    previous = None
        del previous_digest_map
        if parent_revision is None and previous is not None:
            parent_revision = previous.revision_id

        if (not omission_confirmations and previous is not None and previous_key is not None
                and not _article_work_pending(previous_view,curation)
                and not previous.state.passage_failures
                and curr_digests == previous.digest_map
                and _publication_manifest_matches(previous.manifest, manifest)):
            from jonex_wiki.kernel.telemetry import count_work
            # ── pre-publication recheck on the reuse path (S7-B) ─────────
            # "Unchanged" is a claim about the input this run read.  Returning
            # the previous revision republishes it, so the claim is re-proved
            # before it is made: the sources are re-read and the published head
            # is compared with the parent.  A source that moved makes this run
            # a real compile of the current sources; a newer publication that
            # landed meanwhile makes it a refusal below, never an overwrite of
            # someone else's revision with an older one.
            count_work("unchanged_revision_recheck")
            fresh_artifacts = await sources.get_artifacts(context_id, tenant_id)
            fresh_digests, _fresh_inventory = self.freeze_source_inventory(
                fresh_artifacts, manifest)
            head = await self._published_revision_id(store, tenant_id, context_id)
            stale_parent = bool(head and head != parent_revision)
            if dict(fresh_digests) != curr_digests or stale_parent:
                count_work("unchanged_revision_reuse_refused")
                if dict(fresh_digests) != curr_digests:
                    all_artifacts = fresh_artifacts
                    curr_digests = fresh_digests
                    source_inventory = _fresh_inventory
                previous_key = None
                if stale_parent:
                    raise ValueError(
                        "PUBLICATION_PARENT_CHANGED:{}:{}".format(
                            parent_revision, head))
            else:
                count_work("unchanged_revision_reused")
            if not stale_parent and dict(fresh_digests) == dict(
                    previous.digest_map):
                artifacts = RevisionArtifacts(
                    revision_id=previous.revision_id,
                    pages={path: snapshot.model_dump() for path, snapshot in previous.state.pages.items()},
                    claims=[claim.model_dump() for claim in previous.claims], graph=[], ledger=[], errors=[],
                    checksums={}, digest_map=dict(previous.digest_map),
                    source_inventory_digest=previous.source_inventory.digest,
                    runtime_snapshot_checksum=previous.runtime_snapshot_checksum,
                    source_count=len({item.source_id for item in previous.state.source_archive.values()}),
                    page_count=len(previous.state.pages), claim_count=len(previous.claims),
                    error_count=sum(1 for entry in previous.error_book.entries if entry.status == "open"),
                )
                return artifacts, previous_key

        if progress:
            await progress.update("", "delta", 15, "Computing source digests")

        # Parallel compile: passages are scheduled with the manifest-selected
        # scheduler and concurrency.  Both values are deployment knobs and are
        # written explicitly by the caller's manifest builder (platform side:
        # Settings → ManifestBuilder); the engine deliberately keeps no
        # "default concurrency of its own" — a missing key means "the caller
        # declared nothing", and the library baselines in ``pipeline`` apply
        # (serial, stream).  Two copies of one default is how they drift.
        curation_cfg = manifest.get("curation") or {}
        # Physical batching removed (§14 de-batching): every passage issues its
        # own complete() call so scene → call-site stays one-to-one.
        claim_policy = pipeline.resolve_claim_policy(manifest)
        raw_concurrency = curation_cfg.get("compile_concurrency")
        compile_concurrency = (
            pipeline.DEFAULT_COMPILE_CONCURRENCY if raw_concurrency is None
            else max(1, int(raw_concurrency))
        )
        # A passage permanently rejected twice is skipped unless this rate cap
        # is exceeded (then the compile fails closed).
        raw_rate = curation_cfg.get("max_passage_failure_rate")
        if raw_rate is None:
            max_passage_failure_rate = pipeline.DEFAULT_MAX_PASSAGE_FAILURE_RATE
        else:
            max_passage_failure_rate = max(
                0.0, min(1.0, float(raw_rate))
            )
        state, error_book, claims, contribution_set = await self._layered_compile(
            all_artifacts,
            model,
            routing,
            progress,
            store=store,
            tenant_id=tenant_id,
            context_id=context_id,
            manifest=manifest,
            compile_concurrency=compile_concurrency,
            max_passage_failure_rate=max_passage_failure_rate,
            claim_policy=claim_policy,
            has_previous=previous is not None or previous_key is not None,
            telemetry=telemetry,
            cancel=cancel,
            compile_main_prompt=compile_main_prompt,
            current_digests=curr_digests,
            previous_contributions=(
                previous.source_contributions
                if previous is not None
                and getattr(previous, "source_contributions", None) is not None
                else None
            ),
            previous_revision_id=(
                previous.revision_id if previous is not None else ""
            ),
            previous=previous,
            sources=sources,
        )

        if omission_confirmations:
            from jonex_wiki.domain.engine.compile.omission_repair import apply_confirmed_omissions
            from jonex_wiki.domain.engine.compile.content_validation import ClaimDraft, EvidenceRef, validate_claim_evidence
            state, error_book, outcomes = apply_confirmed_omissions(state, error_book, omission_confirmations)
            for outcome in outcomes:
                if not outcome.get("accepted"):
                    continue
                passage = state.source_archive[outcome["passage_id"]]
                start, end = outcome["range"]
                excerpt = passage.text[start:end]
                ref = EvidenceRef(passage_id=passage.passage_id, source_id=passage.source_id,
                    document_id=passage.document_id, chunk_id=passage.chunk_id, excerpt=excerpt)
                added = validate_claim_evidence(state, [ClaimDraft(page_path=outcome["page_path"],
                    text=excerpt, evidence=[ref])]).validated_claims
                claims.extend(claim for claim in added if claim.claim_id not in {c.claim_id for c in claims})
            state._publication = None
        from jonex_wiki.domain.engine.compile.relation_increment import project_state_relations
        projected = await asyncio.to_thread(project_state_relations, state, claims)
        if projected.pages != state.pages:
            state = projected
            state._publication = None

        # Persist directory split decisions before building query/navigation
        # artifacts and before deriving the immutable revision identity.
        from jonex_wiki.domain.engine.compile.rendering import stabilize_directory_shards
        stable_state = await asyncio.to_thread(stabilize_directory_shards, state)
        if stable_state is not state:
            state = stable_state

        if progress:
            await progress.update("", "validate", 85, "Running final validation")

        # Build every persisted index before the bundle enters staging.  Query
        # reopens these published bytes; it never rebuilds from mutable Sources.
        def prepare_publication():
            with telemetry_stage(telemetry, "validation"):
                if state._publication is not None:
                    validated_errors = error_book
                    corpus, claim_audit = state._publication
                    render_signatures = getattr(state, "_render_signatures", None)
                    rendered_pages = getattr(state, "_rendered_pages", None)
                else:
                    from jonex_wiki.domain.engine.compile.publication import (
                        KEEP_AND_SEARCH_RULE_VERSION,
                        reconcile_claim_issues,
                    )
                    from jonex_wiki.domain.engine.compile.raw_collection import (
                        collect_article_and_table_pages,
                        collect_workbook_outlines,
                    )
                    from jonex_wiki.domain.engine.query.retrieval import validate_query_claims
                    with telemetry_stage(telemetry,'claim_validation'):
                        validate_query_claims(
                            state, claims,
                            previous_state=previous.state if previous is not None else None,
                            previous_claims=previous.claims if previous is not None else None,
                        )
                    with telemetry_stage(telemetry,'claim_issue_reconcile'):
                        validated_errors = reconcile_claim_issues(state, claims, error_book)
                    with telemetry_stage(telemetry,'raw_source_pages'):
                        raw_pages = collect_article_and_table_pages(all_artifacts)
                        workbook_outlines = collect_workbook_outlines(all_artifacts)
                    render_signatures = {}
                    rendered_pages = set()
                    corpus = build_query_corpus(
                        state, claims, error_book=validated_errors,
                        include_sources=True, validated=True,
                        source_unit_rule_version=KEEP_AND_SEARCH_RULE_VERSION,
                        raw_pages=raw_pages,
                        workbook_outlines=workbook_outlines,
                        previous_corpus=previous.corpus if previous is not None else None,
                        previous_source_unit_rule_version=previous.source_unit_rule_version if previous is not None else None,
                        previous_render_signatures=previous.render_signatures if previous is not None else None,
                        render_signatures_out=render_signatures,
                        rendered_pages_out=rendered_pages,
                    )
                    claim_audit = pipeline.enforce_claim_policy(state, claims, policy=claim_policy)
            return corpus, claim_audit, render_signatures, rendered_pages, validated_errors

        corpus, claim_audit, render_signatures, rendered_pages, error_book = await asyncio.to_thread(prepare_publication)
        runtime_snapshot_checksum = bundle.snapshot_checksum(
            effective_runtime
        )
        from jonex_wiki.domain.engine.compile import source_contribution
        contribution_set, dangling = _prune_dangling_displays(contribution_set, claims)
        if dangling:
            error_book = _record_contribution_rejections(error_book, dangling, state)
        with telemetry_stage(telemetry, 'contribution_payload'):
            contribution_payload = source_contribution.payload(contribution_set)

        from jonex_wiki.domain.engine.compile.publication import RULE_VERSION as evidence_rule_version
        with telemetry_stage(telemetry, 'publication_identity'):
            publication_checksum = bundle_codec.canonical_checksum({
                'state':{key:value for key,value in state}, 'claims':claims,
                'error_book':error_book, 'source_contributions':contribution_payload,
                'evidence_rule_version':evidence_rule_version})
        revision_id = pipeline.derive_revision_id(
            curr_digests,
            publication_checksum=publication_checksum,
            manifest_checksum=manifest_checksum,
            manifest_payload_checksum=bundle.sha256_hex(
                pipeline.canonical_json(manifest).encode("utf-8")
            ),
            context_id=context_id,
            tenant_id=tenant_id,
            prompt_checksum=pipeline.PROMPT_CHECKSUM,
            engine_id=self.ENGINE_ID,
            engine_version=self.ENGINE_VERSION,
            source_inventory_digest=source_inventory.digest,
            runtime_snapshot_checksum=runtime_snapshot_checksum,
            # LWP-CURATE-RECOVERY (P1-1): scope the revision id to the exact
            # bundle codec version (see derive_revision_id docstring).
            bundle_schema_version=bundle.SCHEMA_VERSION,
        )

        # Build and stage the immutable bundle.  Only the worker may promote
        # the verified candidate and advance the DB Revision pointer.
        counts = {
            "sources": len({p.source_id for p in state.source_archive.values()}),
            "passages": len(state.source_archive),
            "pages": len(state.pages),
            "claims": len(claims),
            "open_errors": sum(1 for e in error_book.entries if e.status == "open"),
            "source_contributions": len(contribution_set.records),
            **claim_audit["summary"],
        }
        repaired_entries = sorted(entry.id for entry in error_book.entries
            if entry.code == "COMPILE_INFORMATION_OMITTED" and entry.resolution == "omission_repaired")
        if repaired_entries:
            import hashlib
            revision_id = hashlib.sha256((revision_id + json.dumps(repaired_entries)).encode()).hexdigest()
        file_references = None
        if (previous is not None and previous_view is not None
                and getattr(previous_view, "is_published_revision", True)
                and getattr(store, "supports_file_references", False)):
            from jonex_wiki.kernel.ports import RevisionFileReference
            file_references = {entry["path"]: RevisionFileReference(
                path=entry["path"], sha256=entry["sha256"],
                read_bytes=lambda path=entry["path"]: previous_view.read_bytes(path))
                for entry in previous_view.manifest["files"]
                if entry["path"].startswith(("wiki/", "json-shards/"))}
        def publication_files(revision_id):
            return bundle_codec.iter_bundle_files(
            revision_id=revision_id,
            artifacts=all_artifacts,
            state=state,
            error_book=error_book,
            claims=claims,
            manifest=manifest,
            counts=counts,
            digest_map=curr_digests,
            tenant_id=tenant_id,
            context_id=context_id,
            source_inventory=source_inventory,
            effective_runtime=effective_runtime,
            rendered_wiki_files=getattr(state, "_rendered_wiki_files", None),
            prepared_corpus=corpus,
            prepared_audit=claim_audit,
            render_signatures=render_signatures,
            previous_reading_index=previous.reading_index if previous is not None else None,
            previous_page_index=previous.corpus.page_index if previous is not None and previous.corpus is not None else None,
            rendered_pages=rendered_pages,
            source_contributions=contribution_set,
            previous_wiki_files=(previous.corpus.files if previous is not None and previous.corpus is not None else None),
            file_references=file_references,
            shard_artifacts=getattr(store, "supports_json_shards", False),
            previous=previous,
            prepared_contribution_payload=contribution_payload,
        )
        key = None
        if agent_compiler is not None:
            if not hasattr(store,'finish_authored_revision') or not hasattr(store,'open_staged_revision'):
                raise ValueError('AGENT_COMPILER_REQUIRES_PRIVATE_CANDIDATE_VIEW')
            keep_source = bool(article_source_identity and not counts.get("failed_passage_count"))
            source_options = {"fingerprints": {"article_source": article_source_identity}} if keep_source else {}
            with telemetry_stage(telemetry, "article_source_staging"):
                candidate = await store.stage_revision(tenant_id, context_id, revision_id,
                    publication_files(revision_id), attempt_id=(article_source_key if keep_source else artifact_attempt_id),
                    parent_revision=parent_revision if previous is not None else None,
                    private_source=True, **source_options)
            view = store.open_staged_revision(candidate)
            revision_id, key = await finish_articles(candidate, view, counts)
        # ── pre-publication parent recheck (S7-B) ────────────────────────
        # Publishing on top of a parent that is no longer the published head
        # would silently overwrite a publication that landed while this compile
        # ran.  The check is a refusal, not a warning; a store that cannot
        # report its head is counted as unavailable rather than assumed safe.
        from jonex_wiki.kernel.telemetry import count_work

        head = await self._published_revision_id(store, tenant_id, context_id)
        if parent_revision is not None and head and head != parent_revision:
            count_work("publication_parent_changed")
            raise ValueError("PUBLICATION_PARENT_CHANGED:{}:{}".format(
                parent_revision, head))
        if previous is None and head:
            count_work("publication_parent_recheck_skipped")
        if key is None:
            with telemetry_stage(telemetry, "bundle_render_and_persist"):
                key = await store.stage_revision(
                    tenant_id, context_id, revision_id, publication_files(revision_id),
                    attempt_id=artifact_attempt_id,
                    parent_revision=parent_revision if previous is not None else None,
                    fingerprints=fingerprints,
                )

        # File generation is lazy: recheck again after the private stage is drained.
        if parent_revision is not None:
            head = await self._published_revision_id(store, tenant_id, context_id)
            if head and head not in {parent_revision, revision_id}:
                count_work("publication_parent_changed")
                raise ValueError("PUBLICATION_PARENT_CHANGED:{}:{}".format(parent_revision, head))

        artifacts = RevisionArtifacts(
            revision_id=revision_id,
            pages={path: snapshot.model_dump() for path, snapshot in state.pages.items()},
            claims=[c.model_dump() for c in claims],
            graph=[],
            ledger=[],
            errors=[],
            checksums={},
            digest_map=dict(curr_digests),
            source_inventory_digest=source_inventory.digest,
            runtime_snapshot_checksum=runtime_snapshot_checksum,
            source_count=counts["sources"],
            page_count=counts["pages"],
            claim_count=counts["claims"],
            error_count=counts["open_errors"],
        )

        if progress:
            await progress.update("", "publish", 100, "Compilation complete")

        return artifacts, key

    # ── compile helpers ───────────────────────────────────────────────

    async def _empty_compile(
        self,
        context_id: str,
        tenant_id: str,
        revision_id: str,
        manifest: dict,
        store: ArtifactStore,
        progress: ProgressSink | None,
        *,
        source_inventory: SourceInventorySnapshot,
        effective_runtime: EffectiveRuntimeSnapshot,
        artifact_attempt_id: str | None,
        telemetry: RuntimeTelemetryRecorder | None = None,
    ) -> tuple[RevisionArtifacts, str]:
        """Stage the empty bundle for a context with no sources."""
        if progress:
            await progress.update("", "publish", 100, "No sources to compile")
        runtime_snapshot_checksum = bundle.snapshot_checksum(
            effective_runtime
        )
        with telemetry_stage(telemetry, "bundle_render"):
            files = bundle_codec.build_empty_bundle_files(
                revision_id=revision_id,
                manifest=manifest,
                tenant_id=tenant_id,
                context_id=context_id,
                source_inventory=source_inventory,
                effective_runtime=effective_runtime,
            )
        with telemetry_stage(telemetry, "bundle_persist"):
            key = await store.stage_revision(
                tenant_id, context_id, revision_id, files,
                attempt_id=artifact_attempt_id,
                fingerprints={"source_inventory": source_inventory.digest,
                              "runtime": runtime_snapshot_checksum,
                              "query_policy": "wiki-agent-v1"},
            )
        artifacts = RevisionArtifacts(
            revision_id=revision_id,
            pages={},
            claims=[],
            graph=[],
            ledger=[],
            errors=[],
            checksums={},
            digest_map={},
            source_inventory_digest=source_inventory.digest,
            runtime_snapshot_checksum=runtime_snapshot_checksum,
            source_count=0,
            page_count=0,
            claim_count=0,
            error_count=0,
        )
        return artifacts, key


    @timed_async_stage('understanding_and_organization')
    async def _layered_compile(
        self,
        all_artifacts: list,
        model: ModelPort,
        routing: dict,
        progress: ProgressSink | None,
        *,
        store: ArtifactStore,
        tenant_id: str,
        context_id: str,
        manifest: Mapping[str, Any],
        compile_concurrency: int = pipeline.DEFAULT_COMPILE_CONCURRENCY,
        max_passage_failure_rate: float = pipeline.DEFAULT_MAX_PASSAGE_FAILURE_RATE,
        claim_policy: object = "production",
        has_previous: bool = False,
        telemetry: RuntimeTelemetryRecorder | None = None,
        cancel: CancelPort | None = None,
        compile_main_prompt: str = "",
        current_digests: Mapping[str, Any] | None = None,
        previous_contributions: Any = None,
        previous_revision_id: str = "",
        previous: "bundle_codec.PreviousBundle | None" = None,
        sources: Any = None,
    ) -> tuple[Any, ErrorBook, list, Any]:
        """``compile_mode=layered``: understand sources, then organize pages.

        Two stages, one publication tail.  Understanding
        (``understanding.py``) answers what each source says and persists that
        answer; organization (``organization.py``) projects those validated
        contributions onto pages inside a bounded window.  Both return the same
        ``(state, error_book, claims)`` state for publication, so
        relations, link resolution, query corpus, indexes, Bundle staging and
        publication remain the *same* code -- not a second engine.

        Unchanged pages are reused only when their source versions and record
        sets still match. Changed pages are rebuilt from current contributions;
        source and publication-parent checks prevent stale reuse.
        """
        from jonex_wiki.domain.engine.compile import organization as organization_module
        from jonex_wiki.domain.engine.compile import understanding as understanding_module
        from jonex_wiki.kernel.telemetry import count_work

        state, passage_order = pipeline.build_state(all_artifacts)
        if hasattr(model, "set_archive"):
            model.set_archive(state.source_archive)
        # ── S7-B: the increment gate ──────────────────────────────────────
        # A page may only be carried from the previous publication when every
        # source that produced it is still live *and* unchanged.  A moved
        # source is re-understood and its page rebuilt; a removed source is not
        # carried at all, so a deleted source cannot survive as a stale page
        # snapshot and no page is ever the union of old and new snapshots.
        live_versions = dict(current_digests or {})
        live_sources = {str(artifact.source_id) for artifact in all_artifacts}
        previous_state = getattr(previous, "state", None)
        # ``digest_map`` is the frozen *artifact* fingerprint per logical
        # source -- the same vocabulary ``current_digests`` uses.
        # ``source_digests`` is the raw-content
        # vocabulary of the Bundle and is not comparable with it.
        previous_versions = dict(
            getattr(previous, "digest_map", None)
            or getattr(previous, "source_digests", {}) or {})
        from .delta import CompileDelta
        with telemetry_stage(telemetry, "archive_delta"):
            state._compile_delta = CompileDelta.between(state, previous_state, live_versions, previous_versions)
            # Archived inputs are read-only during compilation. Sharing exact
            # matches also preserves metadata sharing across source intervals.
            if previous_state is not None:
                for pid, passage in list(state.source_archive.items()):
                    if passage.document_id in state._compile_delta.unchanged_documents:
                        state.source_archive[pid] = previous_state.source_archive[pid]
        moved_sources = set(state._compile_delta.changed_sources)
        removed_sources = set(state._compile_delta.removed_sources)
        reusable_pages: set = set()
        previous_records: dict = {}
        previous_claims: dict = {}
        if previous is not None and previous_state is not None:
            for path, page in previous_state.pages.items():
                owners = {str(sid) for sid in (getattr(page, "source_ids", ()) or ())}
                if owners and not (owners & (moved_sources | removed_sources)):
                    reusable_pages.add(path)
            for record in (getattr(getattr(previous, "source_contributions", None),
                                   "records", None) or ()):
                for display in (getattr(record, "displays", ()) or ()):
                    previous_records.setdefault(
                        str(display.page_path), set()).add(str(record.record_id))
            for claim in (getattr(previous, "claims", None) or ()):
                previous_claims.setdefault(str(getattr(claim, "page_path", "")),
                                          []).append(claim)
            reusable_pages &= set(previous_records) & set(previous_claims)
        if has_previous:
            count_work("layered_incremental_attempted")
            count_work("layered_incremental_sources_moved", len(moved_sources))
            count_work("layered_incremental_sources_removed", len(removed_sources))
            count_work("layered_incremental_pages_reusable", len(reusable_pages))
        if progress:
            await progress.update(
                "", "sources", 30,
                "Understanding and organizing {} passages".format(len(passage_order)),
            )
        stage = understanding_module.UnderstandingStage(
            state,
            all_artifacts,
            model=model,
            store=store,
            tenant_id=tenant_id,
            kb_id=context_id,
            model_name=str((manifest.get("models") or {}).get("curation", "")),
            routing=routing,
            concurrency=compile_concurrency,
            manifest=manifest,
            telemetry=telemetry,
            cancel=cancel,
            compile_main_prompt=compile_main_prompt,
        )
        result = await organization_module.organize_sources(
            state,
            stage=stage,
            concurrency=compile_concurrency,
            max_passage_failure_rate=max_passage_failure_rate,
            claim_policy=claim_policy,
            telemetry=telemetry,
            cancel=cancel,
            reusable_pages=reusable_pages,
            previous_pages=({path: previous_state.pages[path]
                             for path in reusable_pages}
                            if previous_state is not None else None),
            previous_records=previous_records,
            previous_claims=previous_claims,
        )
        understanding = result.understanding
        count_work("layered_understanding_passages", len(stage.order))
        count_work("layered_understanding_cache_hits", understanding.cache_hits)
        count_work("layered_understanding_cache_misses", understanding.cache_misses)
        # Calls are *sent requests*; understandings are the checkpoints they
        # produced.  A failure plus a successful retry is two calls, one
        # understanding -- never one of each.
        count_work("layered_understanding_calls", understanding.model_calls)
        count_work("layered_understanding_understandings", understanding.understandings)
        count_work("layered_understanding_retries", understanding.retries)
        count_work("layered_understanding_attempt_failures", understanding.attempt_failures)
        count_work("layered_understanding_failed_passages", len(understanding.failures))
        count_work("layered_understanding_records", understanding.records)
        count_work("layered_understanding_reused_records", understanding.reused_records)
        count_work("layered_understanding_model_ms", understanding.model_ms)
        count_work("layered_understanding_queue_ms", understanding.queue_ms)
        count_work("layered_understanding_store_ms", understanding.store_ms)
        count_work("layered_understanding_stage_lock_ms", understanding.stage_lock_ms)
        count_work("layered_organization_commit_lock_ms", result.commit_lock_ms)
        count_work("layered_organization_identity_judgments", result.identity_judgments)
        count_work("layered_organization_identity_judgment_applied",
                   result.identity_judgment_applied)
        count_work("layered_organization_identity_judgment_invalid",
                   result.identity_judgment_invalid)
        count_work("layered_incremental_pages_reused", result.pages_reused)
        count_work("layered_incremental_pages_rebuilt",
                   max(0, result.pages_written - result.pages_reused))
        # The stage total, counted exactly once (``organization.py`` only copies
        # it).  The parts below are inside it, never added to it.
        count_work("layered_understanding_ms", result.understanding_ms)
        logger.info(
            "layered compile | passages=%d understood=%d failed=%d records=%d "
            "pages=%d batches=%d organization_model_calls=%d peak_uncommitted=%d "
            "backpressure_waits=%d",
            len(stage.order), result.passages_organized,
            len(understanding.failures), understanding.records,
            result.pages_written, result.organization_batches,
            result.organization_model_calls, result.peak_uncommitted,
            result.backpressure_waits)
        logger.info(
            "layered understanding | calls=%d (understandings=%d retries=%d "
            "attempt_failures=%d cache_hits=%d cache_misses=%d failed_passages=%d "
            "records=%d reused_records=%d) "
            "timing_ms total=%d [model=%d queue=%d store=%d stage_lock=%d] "
            "commit_lock_ms=%d -- total is the sum of per-passage durations "
            "(queue included, so concurrency can push it past wall time); the "
            "bracketed parts are inside it, never added to it or to each other",
            understanding.model_calls, understanding.understandings,
            understanding.retries, understanding.attempt_failures,
            understanding.cache_hits, understanding.cache_misses,
            len(understanding.failures), understanding.records,
            understanding.reused_records, result.understanding_ms,
            understanding.model_ms, understanding.queue_ms,
            understanding.store_ms, understanding.stage_lock_ms,
            result.commit_lock_ms)
        if progress:
            await progress.update(
                "", "aggregate", 60,
                "Organized {} pages from {} passages".format(
                    result.pages_written, result.passages_organized),
            )
        # The contribution ledger: this run's own verified records, reconciled
        # against the previous Bundle.  A source that was deleted is withdrawn,
        # a source that was replaced mid-run has its records refused, and a
        # record still supported by another live source survives with that
        # source's locations.
        from jonex_wiki.domain.engine.compile import source_contribution

        # The versions this run read are the fingerprints of the artifacts it
        # holds for the whole compile: the ledger's vocabulary is the source
        # fingerprint (the same map the previous Bundle stored), while each
        # checkpoint is *additionally* identity-checked against its own artifact
        # when it is loaded, so a stale reading cannot enter here at all.
        live_versions = dict(current_digests or {})
        contributions, rejections = source_contribution.reconcile_publication(
            previous_contributions,
            incoming=result.contributions,
            read_versions=live_versions,
            current_versions=live_versions,
            read_at_revision=previous_revision_id,
        )
        count_work("layered_contributions_published", len(contributions.records))
        count_work("layered_contributions_withdrawn", len(rejections))
        # ── S7-B: publication-time source recheck ─────────────────────────
        # Reuse is a decision about input that had not changed; it is not
        # permission to publish input that moved while this compile ran.  The
        # source port is re-read once, immediately before publication, and a
        # changed (or deleted) source fails the run with the ledger's own
        # stale-task code -- a late old task cannot republish a replaced source
        # or resurrect a deleted one.
        if sources is not None and has_previous:
            count_work("layered_publication_source_recheck")
            fresh_artifacts = await sources.get_artifacts(context_id, tenant_id)
            fresh_digests, _fresh_inventory = self.freeze_source_inventory(
                fresh_artifacts, manifest)
            if dict(fresh_digests) != live_versions:
                stale_sources = sorted(
                    sid for sid in set(fresh_digests) | set(live_versions)
                    if fresh_digests.get(sid) != live_versions.get(sid))
                count_work("layered_publication_source_recheck_stale",
                           len(stale_sources))
                raise ValueError("{}:{}".format(
                    source_contribution.STALE_TASK_CODE,
                    ",".join(stale_sources)))
        # The rest of the Error Book is filled by the *shared* reconciliation in
        # the publication tail (failed passages, unsupported claims).  What the
        # layered chain adds here is what only it knows: contributions it had to
        # withdraw or refuse, so a vanished fact is visible rather than silent.
        book = _record_contribution_rejections(ErrorBook(entries=[]), rejections,
                                               result.state)
        # ── Final validation and bounded repair ────────────────────
        # Layered compilation is a different way to *produce* pages, not a
        # different publication standard: the same bounded repair rounds, the
        # same structural audit and the same publishability gate run before
        # anything is published.
        state, error_book, claims = await self._finalize_layered(
            result.state, book, list(result.claims), model, routing,
            telemetry=telemetry)
        return state, error_book, claims, contributions

    async def _finalize_layered(self, state: Any, book: ErrorBook, claims: list,
                                model: ModelPort, routing: dict, *, telemetry=None
                                ) -> tuple[Any, ErrorBook, list]:
        """Validate and finalize a layered compilation.

        The repair rounds are bounded and make no model call when the book has
        nothing to repair, so a clean layered compile pays only for the one
        structural audit both modes must pass.  A book that stays blocking, or
        a tree the audit rejects, refuses the publication with the existing
        chain's own error -- in either mode.
        """
        from jonex_wiki.domain.engine.compile.error_book import (
            finalize_error_book,
            is_publish_blocking,
        )
        from jonex_wiki.domain.engine.compile.incremental import WikiRenderIndex
        from jonex_wiki.domain.engine.compile.relation_increment import project_state_relations
        from jonex_wiki.kernel.telemetry import count_work

        # Carried snapshots may refer to identities absent from this revision.
        # Refresh the existing relation projection before the structural audit.
        state = await asyncio.to_thread(project_state_relations, state, claims)
        with telemetry_stage(telemetry, "finalize"):
            final = await finalize_error_book(
                state,
                book,
                model,
                render_index=WikiRenderIndex.from_state(state),
                **routing,
            )
        count_work("layered_finalize_rounds", len(final.rounds))
        resolved = 0
        for repair in final.rounds:
            # Calls and accepted rounds are counted apart: a round that asked
            # the model and had its proposal rejected is a repair attempt, not
            # a repair, and a report that conflates them overstates the fix.
            if repair.model_called:
                count_work("layered_finalize_repair_rounds")
            if repair.accepted and repair.model_called:
                count_work("layered_finalize_repair_rounds_accepted")
            resolved += len(repair.resolved_error_ids)
            pipeline.apply_repair_claims(claims, repair)
        count_work("layered_finalize_repair_claims", resolved)
        if not final.publishable:
            blocking = [entry for entry in final.error_book.entries
                        if is_publish_blocking(entry)]
            count_work("layered_finalize_blocked")
            logger.error(
                "layered finalization not publishable | structural_valid=%s "
                "structural_issue_count=%d open_error_count=%d",
                final.structural_report.is_valid,
                len(final.structural_report.issues),
                len(blocking))
            raise pipeline.ProductionCompileError(
                "finalization not publishable: structural_issue_count={} "
                "open_error_count={}".format(
                    len(final.structural_report.issues), len(blocking)))
        return final.state, final.error_book, claims

    async def _published_revision_id(self, store: ArtifactStore, tenant_id: str,
                                     context_id: str) -> str:
        """The revision the store currently publishes, or "" when unknown.

        ``published_revision_id`` is an optional port: the directory-layout
        store implements it, a store that cannot answer is counted -- never
        treated as "no publication exists", which would be an assumption about
        someone else's data.
        """
        from jonex_wiki.kernel.telemetry import count_work

        reader = getattr(store, "published_revision_id", None)
        if reader is None:
            count_work("publication_parent_recheck_unavailable")
            return ""
        try:
            return str(await reader(tenant_id, context_id) or "")
        except NotImplementedError:
            # The port is optional: a store that cannot report its head says so
            # by not implementing it, and that is counted -- never treated as
            # "nothing is published", which would be an assumption about
            # someone else's data.
            count_work("publication_parent_recheck_unavailable")
            return ""



    # ── Phase C ───────────────────────────────────────────────────────

    async def query(self, request, revision, ports, *, cancel: CancelPort | None = None,
                    trace_id: str | None = None) -> QueryRunResult:
        """Bind the Agent to an immutable publication and its model snapshot."""
        if cancel:
            cancel.raise_if_cancelled()
        store = ports["store"] if isinstance(ports, dict) else ports.store

        bundle_key = (
            revision["bundle_key"]
            if isinstance(revision, dict)
            else revision.bundle_key
        )
        context_id = (
            revision["context_id"]
            if isinstance(revision, dict)
            else getattr(revision, "context_id", "")
        )
        tenant_id = (
            revision["tenant_id"]
            if isinstance(revision, dict)
            else getattr(revision, "tenant_id", "")
        )

        from jonex_wiki.kernel.contracts import QueryRequest
        query_request = QueryRequest.model_validate(request)
        question = query_request.question
        run_id = query_request.run_id

        revision_id = (
            revision.get("id") or revision.get("revision_id")
            if isinstance(revision, dict)
            else getattr(revision, "id", None)
            or getattr(revision, "revision_id", None)
        )
        revision_status = (
            revision.get("status")
            if isinstance(revision, dict)
            else getattr(revision, "status", None)
        )
        if revision_status is not None and str(revision_status) != "active":
            raise ValueError("query requires an active published Revision")
        checksum = revision.get("bundle_checksum") if isinstance(revision, dict) else getattr(revision, "bundle_checksum", None)
        direct = (ports.get('agent_direct') if isinstance(ports, dict)
                  else getattr(ports, 'agent_direct', None))
        if direct is None:
            raise ValueError('AGENT_DIRECT_EXECUTOR_REQUIRED')

        def open_snapshot():
            from jonex_wiki.adapters.bundle_view import DirectoryBundleView

            opener = getattr(store, "open_revision", None)
            try:
                view = opener(bundle_key) if opener is not None else None
            except (FileNotFoundError, NotADirectoryError) as exc:
                raise ValueError("AGENT_DIRECT_REQUIRES_LOCAL_DIRECTORY_BUNDLE") from exc
            if not isinstance(view, DirectoryBundleView) or not view.is_published_revision:
                raise ValueError("AGENT_DIRECT_REQUIRES_LOCAL_DIRECTORY_BUNDLE")
            if view.manifest_hash != bundle_key or (checksum and view.manifest_hash != checksum):
                raise ValueError("bundle checksum does not match the published Revision")
            if (view.tenant_id, view.context_id) != (tenant_id, context_id):
                raise ValueError("bundle owner does not match the published Revision")
            if revision_id is not None and view.revision_id != revision_id:
                raise ValueError("bundle revision does not match the published Revision")
            view.verify_manifest_identity()
            view.verified_file_path("revision.json")
            view.verified_file_path("provenance.json")
            provenance = json.loads(view.read_bytes("provenance.json"))
            if (provenance.get("tenant_id"), provenance.get("context_id"), provenance.get("revision_id")) != (
                tenant_id, context_id, view.revision_id
            ):
                raise ValueError("bundle provenance owner or revision mismatch")
            return view, provenance["effective_runtime"]["models"].get("query")

        directory_view, snapshot_model = await asyncio.to_thread(open_snapshot)
        if cancel:
            cancel.raise_if_cancelled()
        query_model = _resolve_query_model(snapshot_model)
        image_options = ({'query_context': query_request.query_context.model_dump(mode='json')}
                         if query_request.query_context is not None else {})
        return await direct.query(view=directory_view,
            question=question, model=query_model, tenant_id=tenant_id,
            context_id=context_id, run_id=run_id, revision_id=directory_view.revision_id, cancel=cancel,
            **image_options, **({'trace_id': trace_id} if trace_id else {}))

    async def inspect(self, revision, ports=None) -> dict[str, str]:
        """Inspect a published revision's decoded bundle content tree.

        ``revision`` is a ``WikiRevision`` (or dict) carrying ``bundle_key``
        plus the ``ports`` store, or an embedded ``bundle_bytes`` value.
        """
        store = None
        if ports is not None:
            store = ports["store"] if isinstance(ports, dict) else ports.store

        if isinstance(revision, dict) and "bundle_bytes" in revision:
            bundle_bytes = revision["bundle_bytes"]
        elif store is not None:
            bundle_key = (
                revision["bundle_key"]
                if isinstance(revision, dict)
                else revision.bundle_key
            )
            if hasattr(store, "open_revision"):
                try:
                    view = store.open_revision(bundle_key)
                    tree = {}
                    for path in view.list_entries():
                        data = view.read_bytes(path)
                        try:
                            tree[path] = data.decode("utf-8")
                        except UnicodeDecodeError:
                            tree[path] = "<binary bytes={} sha256={}>".format(len(data), bundle.sha256_hex(data))
                    return tree
                except (ValueError, FileNotFoundError, NotADirectoryError):
                    pass
            bundle_bytes = await store.get_bundle(bundle_key)
        else:
            raise ValueError("inspect requires a store or embedded bundle_bytes")

        decoded = bundle.decode_bundle(bundle_bytes)
        tree = {}
        for path, data in sorted(decoded.files.items()):
            try:
                tree[path] = data.decode('utf-8')
            except UnicodeDecodeError:
                tree[path] = '<binary bytes={} sha256={}>'.format(len(data), bundle.sha256_hex(data))
        return tree
