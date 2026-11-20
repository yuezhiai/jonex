# Changelog

All notable changes to Jonex Platform will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project intends to follow [Semantic Versioning](https://semver.org/). Release sections are dated by public release; version tags have not yet been adopted.

## [2026-11-20]

### Added

- Ontology reasoning: axioms and reasoning rules with YAML contracts, rule authoring, conflict detection and reasoning views.
- Task center: unified knowledge-base parse/compile definition (three-stage `start`/`steps`/`end`), task definition editor, execution list, run and settings dialogs.
- Jonex Wiki engine: agent compile adapters, authoring batches, coherent catalog, reading projection, source identity and heading references, table quality and navigation, PDF table parsing and direct-file evidence.
- Wiki agent runtime deployment: runtime setup and preflight scripts, seccomp profile, AppArmor policy and `make wiki-check`.
- Knowledge search: no-answer detection, wiki trace model, presentation helpers and session runtime store.
- Gateway and MCP server wiki page read regression coverage and ontology degradation handling.

### Changed

- Knowledge-base document parsing, reconciliation, wiki delivery and ingest backend dispatch refreshed.
- LightRAG: connection pool rebuild, retry policy, health probe, chunk limit and document anchor index.
- PostgreSQL updates: task step concurrency, ontology axioms and reasoning rules, task definition management, last-step tristate and LightRAG chunk index.
- Deployment service wiring, environment templates and wiki runtime configuration refreshed.

### Fixed

- Document archive and delete alignment, provenance degradation, Neo4j unavailability and sidecar invoke status handling.
- Monitor acceptance, schema generation backend and search history classification fixes.

## [2026-11-17]

### Added

- Data governance approvals, classification and standard catalog updates.
- Task center runtime scheduling and task result updates.
- Schema generation for ClickHouse and InfluxDB v3 sources, with per-knowledge-base model settings.
- NL2SQL data search, evolution and retrieval metrics updates.
- Document versions, similarity, image queries and wiki delivery updates.
- Search answers with citations and evidence viewers.
- Ontology canvas, document detail, ETL and knowledge-base UI updates.
- Jonex Wiki compile, media and retrieval updates; RAG parsing and VLM updates.
- Template scenario and ecosystem updates.
- Platform authentication, roles, audit and MCP administration updates.
- Gateway and MCP server updates; shared frontend shell, end-to-end and build updates.
- Local development and inspection utilities, including landing A/B evaluation.

### Changed

- PostgreSQL migrations reorganized to one file per database/subsystem (001–010, 090 seed data, 091 comments).
- Deployment service wiring, environment templates and build configuration refreshed.
- Project metadata and dependency baselines refreshed.

### Fixed

- Knowledge-base ingestion, parsing, reconciliation and task hardening.
- Core configuration, lifecycle and gateway hardening.

## [2026-11-05]

### Added

- Platform monitoring: call, task and token dashboards with filters, trends and object drill-downs.
- Data governance console: approvals, classification, clearance, standard catalog, lineage and retention.
- Tenant security configuration and audit administration.
- Task center: refined runtime scheduling, subtask results and error reporting.
- Standalone ontology compile service with backend dispatch, schema generation and per-knowledge-base model settings.
- NL2SQL data search with evolution loop and retrieval metrics.
- Document versions, document similarity, image queries and wiki input delivery.
- Knowledge search answers with citations, evidence drawers and a PDF bbox viewer.
- Ontology canvas editor for schema modelling.
- Jonex Wiki engine updates for compilation, media and retrieval.
- Template scenario versions and publish approval flow.
- Platform authentication, roles, audit and MCP administration updates.
- Gateway and MCP server updates; shared frontend shell, routing and end-to-end coverage.

### Changed

- Deployment service wiring, environment templates and build configuration refreshed.
- Project metadata, dependency baselines and local development scripts refreshed.

### Fixed

- Knowledge-base ingestion, parsing, reconciliation and task hardening.
- Core configuration, lifecycle and gateway hardening.

## [2026-10-28]

### Changed

- READMEs introduce the Jonex Wiki sub-project, its OKF-based LLM Wiki engine and its capabilities.

## [2026-10-21]

### Added

- Platform monitoring with call, task and token views.
- Data governance catalog, classification, lineage and retention console.
- Tenant security configuration and audit administration.
- Task center runtime scheduling, subtask results and error reporting.
- Standalone ontology compile service with backend dispatch.
- Document versions, image queries and wiki search sessions.
- Wiki answers with citations, evidence drawers and bbox citation viewer.
- Document quality badges and detail-page tab structure.
- Template scenario versions and publish approval flow.
- Jonex Wiki query progress, evidence views and compile pipeline updates.
- Engine chunk ingestion, parsing and enqueue-ordering updates.
- Profile secret rotation and audit tooling.
- Document, knowledge-base and capability API routes.
- Shared shell routing, i18n and session updates.
- Knowledge-base pages and navigation updates.
- Platform-management menu, tenant and operations UI updates.

### Changed

- Deployment service wiring and environment templates refreshed.

### Fixed

- Knowledge-base ingestion, reconciliation and ontology service hardening.
- Core configuration, lifecycle and gateway hardening.

## [2026-10-10]

### Added

- Video analysis pipeline with OCR stack.
- Document parser pipeline with office and table extraction.
- Native ontology extraction with graph writes.
- Keycloak OIDC single sign-on with per-tenant realms.
- External identity integration with scheduled user sync.
- Application shell, space switcher and dashboard.
- Shared UI library, session SDK and end-to-end coverage.
- Data operations console and administration UI.
- Template center and MCP service directory.
- Environment-driven model resolution for OpenKB and LightRAG.
- Task center runtime, scheduling and permission-controlled file processing.
- Per-knowledge-base ingest backend dispatch.
- Jonex Wiki engine integration with outline and page references.
- Knowledge compilation, wiki reading and search UI.
- Optional self-hosted MinerU parser service (Compose profile) with deployment documentation.

### Changed

- Deployment configurations and database baseline refreshed.

### Fixed

- Token handling, internal keys and outbound request hardening.

## [0.1.0] — 2026-09-22

Initial open-source release.

### Added

- Core platform: API gateway, sidecar, capability services, PostgreSQL, Redis, Milvus, Neo4j and object storage (S3/MinIO/COS).
- RBAC with fine-grained permission codes, tenant-aware login, impersonation and user/role management.
- MCP server, MCP key management and knowledge-base retrieval.
- Knowledge-base spaces, quotas, batch document operations and search history replay.
- Table parsing and chunk governance, text block packing, image reference chain and answer feedback.
- OpenKB / LLM-Wiki schema compilation, document quality signals and retrieval scope controls.
- Deployment assets: nginx HTTP/HTTPS modes, PostgreSQL baseline and incremental migrations.
- Apache License 2.0 and open-source community governance files.
- Explicit `SIDECAR_API_KEY` configuration for service-to-service authentication.

### Changed

- Public configuration examples now use safe placeholders.
- Sidecar API-key validation requires an exact configured key.
- Experimental and extension-point capabilities are documented according to their current maturity.
- Legacy product branding renamed to Jonex across UI and code.

### Fixed

- Security hardening: test-token bypass gated behind `ENABLE_TEST_TOKENS`, gateway API-key sentinel with startup guard, and placeholder defaults for database/Neo4j/MinIO credentials.
- Gateway routing, MCP authorization, frontend request handling and rendering fixes.

### Removed

- Internal planning documents, temporary comparison projects, runtime artifacts and legacy modules from the public source tree.
