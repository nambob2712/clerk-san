# Clerk-san codebase summary

This document describes the current repository state only. It does not describe earlier
versions, migration history, or superseded plans.

## Overview

Clerk-san is a local-first document review application for receipts, invoices, recurring bills,
and supported office documents. A source file is preserved before processing. Local OCR and
structured extraction produce a reviewable record; only an approved record becomes searchable,
exportable, or eligible for recurring-bill projections.

The runtime keeps documents and model calls on the local machine or private Compose network.

## Architecture

The application has two core runtime processes, an optional isolated parser, and a static browser
presentation:

1. **FastAPI API** — `clerksan.api.main:app`
   - Serves the HTTP API and health/readiness endpoints.
   - Validates uploads and review commands.
   - Writes database state and enqueues durable jobs.
   - Does not claim or execute worker jobs.

2. **Durable worker** — `python -m clerksan.ingest.worker`
   - Leases queued jobs from the database.
   - Runs ingestion, OCR, extraction, normalization, indexing, and derivative handlers.
   - Uses lease renewal, retry/backoff, terminal failure states, and source-bound idempotency.
   - Defaults to one concurrent worker on the local CPU-oriented deployment.

3. **React UI** — `web/`, built by Vite and served by FastAPI
   - Uses only relative same-origin requests to the local API.
   - Does not connect directly to PostgreSQL, Ollama, or the document store.
   - Presents separate **Upload file** and **Scan bill** intents, mapping, multi-record review,
     verified history, search, and recurring-bill screens.
   - Switches UI-owned copy between English, Vietnamese, and Japanese without translating
     documents, API payloads, or model-generated answer text.

4. **Universal parser sidecar** — `python -m clerksan.ingest.parser_service serve`
   - Exists only under the gated Compose universal profile.
   - Receives an already-open source descriptor over a private Unix socket.
   - Has no network, database, model, secret, or document-store access and runs with container
     resource/capability limits.

Compose runs PostgreSQL and Ollama as private data services. The API is published only on
`127.0.0.1:8000`; the worker has no host port. The browser opens the built UI from that same
loopback origin.

React is the local UI default. The separate processing-mode default remains
`CLERKSAN_INTAKE_MODE=legacy`, which advertises no universal process capabilities. Universal
activation requires successful sidecar probes, a fresh matching API/worker capability lease, and
no queued/running legacy-profile jobs.

## Document data flow

```text
Upload file | Scan bill
  → receive/intent/content safety boundary
  → reject unsafe/ambiguous input, or admit an immutable source intake and original
  → process | store_unprocessed
  → normalized source or source-bound tabular rows
  → extraction batch and candidate decisions
  → human mapping/review
  → atomic activation of the approved cohort
  → verified-only search, exports, duplicate evidence, and bill projections
```

The legacy adapter set remains available through the compatibility path. In universal mode,
`GET /capabilities` is the exact authority for processable formats; documentation does not maintain
a second format inventory. Positively identified safe content may be preserved as
`store_unprocessed` when an adapter/decoder is unavailable. Active, encrypted, executable,
audio/video, malformed, ambiguous, over-limit, and intent-mismatched inputs fail closed. Unknown
containers never fall through to preserve-only handling.

PDF preview authority is exact-source bound. Raw PDFs are attachment-only; the React viewer uses
only complete manifest-linked inert PNG page derivatives or displays a typed preview-unavailable
state. A partial or cross-source page set is never published as a preview.

Replacing an original appends a new source version. Existing source files remain immutable.
Pending work tied to an older source becomes stale, and approval uses an extraction version check
so an outdated review cannot become verified.

## Persistence and integrity

SQLAlchemy async models and migrations cover:

- documents and immutable source-file versions;
- source-intake status/idempotency and worker capability leases;
- normalized artifacts and embedded media;
- immutable mapping sets and entries/ignores;
- extraction batches, candidates, append-only decision revisions, and active cohorts;
- extracted and verified records, including candidate financial subtype;
- durable jobs and leases;
- search chunks and embeddings;
- spreadsheet staging rows;
- exact-source PDF preview manifests and page derivatives;
- issuers and recurring-bill versions;
- duplicate evidence;
- append-only audit history.

PostgreSQL is the production persistence target and uses pgvector, trigger-owned audit history,
append-only source-file guards, and migration-managed schema changes. SQLite is used for the
local demo and tests; it does not prove PostgreSQL trigger, pgvector, or production restore
behavior.

Stored artifacts are content-addressed and checksum-verified before reuse or download. Path
resolution is constrained beneath the configured document store. Backup tooling writes and
verifies manifests and logical inventories. Compose backup/restore has ordinary and maintenance
modes; maintenance keeps the writer fence closed. Every restore requires explicit destructive
confirmation.

## Local models and configuration

`clerksan.config.Settings` reads `CLERKSAN_*` environment variables and `.env` values. The
important settings are:

- database URL and explicit PostgreSQL password;
- Ollama URL;
- storage directory and loopback API URL;
- static legacy/universal intake mode and parser socket timeout;
- OCR engine: `vision_llm`, `yomitoku`, or `paddleocr`;
- extraction, routing, OCR, and embedding model names;
- pinned embedding digest and 768-dimensional vector size;
- worker concurrency, lease, retry, and backoff limits;
- pre-parse request size/shape/concurrency limits, including the 10-second next-chunk receive
  timeout `CLERKSAN_REQUEST_RECEIVE_TIMEOUT_SECONDS`;
- upload, PDF, image, text, table, structure, recursion, and archive safety bounds;
- review-confidence, reminder, and anomaly thresholds;
- demo mode and SQL logging switches.

The demonstrated local path uses Ollama models `gemma3:4b`, `qwen2.5:7b`, and
`nomic-embed-text:v1.5`. PaddleOCR and YomiToku are optional Python runtimes and are not base
dependencies. `scripts/local-app.sh` performs a locked local UI build, then owns the no-Docker
API and worker lifecycle for an existing synthetic SQLite demo; it checks but never starts or
stops host-owned Ollama.

## Public API surface

The API exposes:

- `/health`, `/ready`, and `/capabilities`;
- intent-keyed source intake and status endpoints;
- document upload, listing, detail, original download, status, source replacement, reprocess,
  and derivative retry;
- source-bound mapping/schema endpoints and paginated candidate batches;
- singleton-compatible and batch review/activation endpoints with stale-vector conflicts;
- exact-source PDF preview manifest/page endpoints;
- local query and hybrid retrieval endpoints;
- recurring-bill listing, analysis, reminders, and payment updates;
- accounting and audit CSV exports.

Review approval is the boundary between extracted data and verified data. Aggregations use typed
verified-record SQL paths; semantic retrieval provides citations and does not perform arithmetic.

Core readiness fails only for unsafe database/schema/storage state. Missing local models are a
processing-only condition: `processing_ready` becomes false and dependent work remains queued
without converting safe intake/review into a core outage. Universal process advertisement is
separately withdrawn when its sidecar evidence or matching worker lease is unavailable.

The native launcher runs API and worker with demo mode disabled and treats `/ready` as the authority.
It reports core and processing state separately, waits for a fresh worker lease, and never pulls host
models. Before a possible SQLite compatibility upgrade, it creates and verifies a rollback snapshot
under the ignored launcher runtime, then records the schema state only after core readiness succeeds.

## Current test and verification state

The suite covers API contracts, repositories and migrations, ingestion adapters, worker/job
state transitions, extraction schemas, search/query, recurring bills, exports, backup tooling,
UI contracts, deduplication, and configuration. PostgreSQL trigger/schema tests and the Compose
infrastructure check require a Docker-compatible runtime.

Primary validation commands:

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python --require-hashes --no-deps -r requirements.lock
.venv/bin/python -m pytest -q
.venv/bin/ruff check clerksan tests eval scripts
bash -n scripts/backup.sh scripts/restore.sh
git diff --check
```

`requirements.lock` is the runnable/test authority; `requirements.txt` is only its reviewed source
input when dependencies are intentionally regenerated.

The no-Docker demo commands are:

```bash
scripts/local-app.sh init-demo
scripts/local-app.sh start
```

`init-demo` accepts only an absent or empty destination and never resets data. `start` opens the
existing demo on loopback and can be stopped safely with `scripts/local-app.sh stop`.

## Current limitations and release boundaries

- Docker/Compose startup, live PostgreSQL trigger enforcement, pgvector planner behavior, and a
  PostgreSQL backup/restore drill require a Docker-capable host and are not established by the
  SQLite demo.
- Universal processing remains release-blocked until that host also proves the parser-sidecar
  sandbox and activation lease/job-drain gates. The static default remains `legacy`.
- `sbom/source-dependencies.spdx.json` is a source lockfile inventory, not a built-image or native
  operating-system SBOM. Those release artifacts remain external gates.
- The staged privacy gate requires explicit approval for every report and may use only an ignored,
  untracked local pattern file for private-source matching.
- The system has no user authentication, tenant isolation, or role-based authorization. Its
  intended security boundary is loopback/private-network deployment.
- PaddleOCR/YomiToku installation, Japanese fixture validation, offline model startup, and
  target-host resource behavior require separate runtime validation.
- Scanner-preservation procedures, retention ownership, scheduled off-machine backups, and
  professional compliance review are operational requirements outside the codebase.
- The product language is compliance-supporting, not a legal certification or guarantee.

## Primary references

- [README](../README.md) — short overview and local proof.
- [Developer preview](developer-preview.md) — supported setup, readiness, data, and publication boundary.
- [Changelog](../CHANGELOG.md) — user-visible developer-preview changes.
- [Run and demo guide](../RUN_AND_DEMO.md) — full local and Compose procedures.
- [Final setup guide](../FINAL_SETUP_AND_RUN.md) — concise operator handoff.
- [Compliance evidence matrix](compliance_denchoho.md) — controls and open operational gates.
- [Frontend decision](decisions/local-first-react-frontend.md) — browser authority and cutover rule.
- [Source dependency SBOM](../sbom/README.md) — inventory scope and image/native limitation.
