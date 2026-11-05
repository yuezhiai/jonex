# Changelog

All notable changes to Jonex Platform will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project intends to follow [Semantic Versioning](https://semver.org/). Release sections are dated by public release; version tags have not yet been adopted.

## [Unreleased] — 2026-11-05

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
