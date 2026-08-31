# Run Clerk-san locally

This guide has two paths:

1. A no-Docker demonstration that runs the real local models against a synthetic receipt.
2. The full local stack with PostgreSQL, Ollama, API, worker, and built React UI.

Both paths use local OCR, extraction, routing, and retrieval models.

## Start from the repository root

The relative paths below are deliberately rooted in this checkout. From anywhere inside
the Git working tree, run:

```bash
cd "$(git rev-parse --show-toplevel)"
```

## Prerequisites

- macOS, Linux, or another host with Python 3.11.
- [uv](https://docs.astral.sh/uv/) to provision Python dependencies.
- Node 24 LTS with npm 11 to build the local browser bundle.
- [Ollama](https://ollama.com/) running locally for OCR, extraction, and embeddings.
- Docker Desktop or a compatible Docker runtime only for the PostgreSQL production-like
  stack.

If `ollama list` cannot reach a local service, start it in another terminal with
`ollama serve` before pulling models or running the no-Docker demo.

Pull the selected local models once:

```bash
ollama pull gemma3:4b
ollama pull qwen2.5:7b
ollama pull nomic-embed-text:v1.5
```

Those commands provision the host Ollama service used by the no-Docker demo. The Compose
stack has its own Ollama volume and its own pull commands below.

### Locked Python dependencies

`requirements.lock` is the runnable dependency authority. To prove a clean install, create a
fresh environment and require every transitive package to match a committed hash without resolving
additional dependencies:

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python --require-hashes --no-deps -r requirements.lock
```

`requirements.txt` is the human-maintained source input used only when deliberately regenerating
and reviewing the lock. Runtime, evaluation, and test commands below consume `requirements.lock`.
The configured embedding tag, digest, and dimension are owned by `clerksan/config.py`; changing that
contract requires a schema migration and a full re-embedding pass.

## Current evidence scope

The checked-in validation commands cover the hash-locked local launcher, React/API contracts,
synthetic workflows, source-dependency SBOM generation, and privacy-tool behavior. They are
local/source checks. They do not prove a container build, sidecar sandbox on the release host,
PostgreSQL behavior, a PostgreSQL restore drill, or an image/native-package SBOM. Reproduce the
relevant checks from the [README validation section](README.md#validation) instead of relying on
retained local plan reports, which are not part of the public source candidate.

## Fastest local UI: no Docker

From the repository root, initialize a fresh synthetic demo once:

```bash
scripts/local-app.sh init-demo
```

`init-demo` creates a synthetic, non-personal receipt and executes this real workflow:

```text
preserve original → local vision OCR → local extraction → pending review
→ approve → verified record → local semantic search
```

Inspect the result:

```bash
.venv/bin/python -m json.tool .clerksan-demo/demo-result.json
```

The demo stores a SQLite database and document files under `.clerksan-demo/`. The launcher
rejects a non-empty directory and never passes `--reset`, so a previous demo is never
replaced silently. To use an empty non-default destination, pass `--data-dir` to
`init-demo`.

## Reproduce local evaluation evidence

The commands below use only deterministic, non-personal fixtures. The first command checks
fixture integrity; it does not claim extraction accuracy without model predictions.

```bash
.venv/bin/python -m eval.run_eval
.venv/bin/python -m eval.benchmark_ocr \
  --out .clerksan-eval-results/ocr-benchmark.md
.venv/bin/python -m eval.benchmark_embeddings \
  --out .clerksan-eval-results/embedding-decision.json
.venv/bin/python -m eval.benchmark_extraction_models \
  --out .clerksan-eval-results/extraction-model-decision.json \
  --baseline-out .clerksan-eval-results/extraction-baseline.json
```

The OCR report can legitimately list optional local engines as unavailable. Its selection is
made only among available candidates, while every report records the exact model artifact and
the observed benchmark-process and Ollama-model residency measurements on the current host.
The shown extraction command is the first-run baseline bootstrap. Repeat runs must pass the existing
ignored baseline as both `--baseline` and `--baseline-out`; follow the exact procedure in
[`eval/fixtures/README.md`](eval/fixtures/README.md).

## Open the demo in the UI

Use the database created by the previous command:

```bash
scripts/local-app.sh start
```

Open `http://127.0.0.1:8000`. The launcher performs a locked local React build, then starts the
loopback API and durable worker. It checks Node/npm, `uv`, host Ollama, the three required
models, and unknown port occupants; it does not start or stop your host-owned Ollama service.

Useful recurring commands:

```bash
scripts/local-app.sh status
scripts/local-app.sh stop
scripts/local-app.sh start --data-dir .clerksan-existing-demo
```

`--data-dir` selects an existing SQLite demo; it must contain `clerksan.sqlite` and
`doc_store/`. The launcher records only its own API/worker PIDs and refuses to stop an
unknown process. Its local logs and PID records are stored under `.clerksan-runtime/`.
When an older local demo is opened, the launcher creates and verifies a rollback snapshot under
`.clerksan-runtime/sqlite-upgrade/pre-upgrade-backups/` before the API applies its reviewed additive
SQLite compatibility upgrades. It records the resulting schema only after normal-mode core readiness
succeeds; it never resets the database or document store. If that upgrade cannot be applied,
`/ready` reports `local_data_needs_upgrade` and data routes return the same structured `503` response
instead of a generic server error. The ignored rollback directory can contain local document data and
must never be committed.

### Retained Streamlit fallback

The React UI is the supported default. Streamlit remains runnable as a manual fallback while the
[cutover rule](docs/decisions/local-first-react-frontend.md#cutover-rule) is still pending. With
the loopback API from the launcher still running, start the fallback in a separate terminal:

```bash
CLERKSAN_API_URL=http://127.0.0.1:8000 \
  .venv/bin/python -m streamlit run app.py \
  --server.address 127.0.0.1 --server.port 8501
```

Open `http://127.0.0.1:8501` and stop the fallback separately with `Ctrl-C`; it is not
launcher-managed. This manual local server is not a Compose/image rollback and does not satisfy
the hard cutover gate: retain Streamlit until the 20% benchmark and immutable container rollback
checks in the decision record are complete.

The local UI shows intake, verified history, review queue, search, and recurring-bill screens.
It displays separate **Upload file** and **Scan bill** controls, but this no-Docker launcher
remains intentionally limited to the legacy parser set. It rejects
`CLERKSAN_INTAKE_MODE=universal` because a normal host process does not provide the required
no-network, read-only-root parser sandbox. Use the Compose universal procedure below for CSV,
TSV, additional office/document families, safe archives, and secondary raster formats.
Verified History labels documents by their canonical
`Expense type` (for example, `electricity`) and keeps amounts separated by currency.
Each review item has an “Open preserved original” link that uses the loopback API and only
serves files inside the configured document store.
The supported launcher starts API and worker with `CLERKSAN_DEMO_MODE=false`. It parses `/ready`,
waits for core availability, and then waits a bounded time for a fresh worker lease. Missing OCR or
extraction tags are availability failures; the configured `nomic-embed-text:v1.5` digest is also
integrity-enforced. The launcher does not automatically pull or replace any model. SQLite does not
emulate the PostgreSQL trigger-owned audit guarantee, so validate audit history in the full Compose
stack before relying on it operationally.

## Intake intents and dispositions

The two buttons are distinct product intents, not alternate styling for the same request:

| Intent | Use it for | Authority boundary |
| --- | --- | --- |
| **Upload file** | Generic information files and, after gated universal activation, tabular mapping and multi-record review. | Candidates remain staged until the user confirms mapping, decisions, and atomic activation. |
| **Scan bill** | One receipt or invoice image/PDF. | Non-image/PDF content fails with an intent mismatch rather than silently changing workflows. |

`CLERKSAN_INTAKE_MODE=legacy` remains the default and publishes no universal process
capabilities. When universal mode has passed its activation gate, `/capabilities` is the runtime
authority for exact process formats:

| Universal disposition | Meaning |
| --- | --- |
| `process` | The exact format is advertised by the verified sidecar and can be queued. |
| `store_unprocessed` | Positively identified safe content is preserved when a decoder or adapter is unavailable; the reason remains visible for later retry. Unknown containers do not use this fallback. |
| `reject` | Active, encrypted, executable, audio/video, malformed, ambiguous, over-limit, or intent-mismatched input is not admitted for processing. |

## Language support

The sidebar can switch the Clerk-san interface during a session between **English**, **Tiếng
Việt**, and **日本語**. The setting changes labels, buttons, helper text, status/empty states,
and local recovery notices; it does not change the document data underneath.

| Surface | Current behavior |
| --- | --- |
| UI chrome | English, Vietnamese, and Japanese are supported. |
| Uploaded originals, filenames, extraction values, audit history | Preserved exactly as received; never translated by the UI. |
| OCR/extraction and search input | Existing local model behavior; language coverage depends on the selected models and document/query content. |
| Backend-generated query answer prose | Shown exactly as returned; this UI-localization pass does not translate it. |

Every UI locale uses the same local-only processing route.

## Replace a source without losing history

Never overwrite a stored document file. To correct a wrong or incomplete source, append a
new immutable original version through the loopback API:

```bash
curl --fail --request POST \
  "http://127.0.0.1:8000/documents/<document-id>/original?actor=local-user" \
  --form "file=@replacement.png"
```

The response returns the appended `version` and a queued job. The previous original remains
in `GET /documents/<document-id>` with its checksum, path, MIME type, and source filename;
the detail audit history also records the new file version. `GET /documents/<document-id>/original`
and worker processing always use the highest original version. Replacing a source supersedes
any still-pending extraction, so an approval screen opened for that old extraction returns
`409 stale_extraction` rather than verifying the wrong source. A previously approved record
stays visible until the replacement extraction is approved.

To run the current original again after rejecting an extraction, or deliberately reprocess a
verified document, request a source-bound job instead of editing any file row:

```bash
curl --fail --request POST \
  "http://127.0.0.1:8000/documents/<document-id>/reprocess?actor=local-user"
```

The endpoint accepts rejected or verified lifecycle states and also recovers a terminal
source-processing failure. It deduplicates repeated requests for the same original and
review version; each retry after a terminal failure is tied to the failed job so it can
make a new attempt without duplicating an active one. If a newer source version arrives
before an older queued job starts, that older job completes without producing an extraction.
The browser review workspace submits this reprocess request after a rejection.

## Retry a terminal background derivative without reprocessing review

If local semantic indexing or embedded-media OCR reaches its configured retry limit, correct
the underlying local cause first (for example, restore the embedding model or a checksum-valid
artifact), then retry only the failed current-source derivative:

```bash
curl --fail --request POST \
  "http://127.0.0.1:8000/documents/<document-id>/retry-derivatives"
```

This keeps the original `dead` job and its error as durable evidence, creates one source-bound
successor per terminal index/OCR input, and does not supersede an extraction or change review
state. Repeating the request while a successor is queued, running, or done returns
`nothing_to_retry`; if that successor later reaches its limit, a new explicit retry receives a
new recovery key. The Scan tab exposes the matching local action for the most recently uploaded
document: source reprocessing for a terminal source-processing error, or derivative recovery for
terminal indexing/OCR work.

## Full local stack with PostgreSQL

The no-Docker demo does not exercise Compose or PostgreSQL. Treat the following as an
operator procedure to validate on your Docker-capable host; do not rely on it until `/ready`
and a restore drill both succeed there.

The optional isolated parser dependency uses the Compose `required: false` service contract;
use Docker Compose 2.20 or newer.

`POSTGRES_PASSWORD` is required by the database, API, and worker services. Choose a
strong local password without committing it to a file, then start only the data services.
This lets you install the required local models before the API readiness check starts:

```bash
export POSTGRES_PASSWORD='choose-a-long-local-password'
docker compose up -d db ollama
```

Pull the selected models into the Compose-managed Ollama volume:

```bash
docker compose exec ollama ollama pull gemma3:4b
docker compose exec ollama ollama pull qwen2.5:7b
docker compose exec ollama ollama pull nomic-embed-text:v1.5
```

Now start the API and worker:

```bash
docker compose --profile app up -d --build
```

That command keeps the default `legacy` intake behavior. To validate the gated universal path in a
fresh isolated environment, start the parser and pass one static mode to both API and worker:

```bash
export CLERKSAN_INTAKE_MODE=universal
docker compose --profile universal --profile app up -d --build
```

On an existing legacy stack, first let every queued/running legacy job finish, then recreate
the API and worker with the universal profile. Never relabel an existing legacy job as
sandboxed work. The parser has no network, database, model, secret, or document-store mount;
the API passes only an already-open source descriptor over its private Unix socket.

Setting the environment variable is not activation proof. Universal processing is available only
after all sidecar startup probes pass, the API observes a fresh worker lease with matching registry
and capability digests, and no queued/running legacy-profile job remains. A missing, stale, or
mismatched lease removes the advertised process list and never falls back to an in-process parser.

Verify the application:

```bash
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/ready
curl --fail http://127.0.0.1:8000/capabilities
docker compose --profile app ps
```

In universal mode, do not upload broad formats until `/ready` reports
`universal_processing_ready: true` and `/capabilities` reports a non-empty `process` list.
If readiness reports an embedding-digest
mismatch, the mutable model tag did not resolve to the pinned model. Do not change the
configured embedding pin merely to bypass that check; reacquire the pinned model, or make an
explicit schema migration followed by full re-embedding.

The current repository evidence does not close the universal release gate: container build and
sidecar probes, PostgreSQL execution, and a real PostgreSQL restore drill still require a
Docker-capable release host. Keep `legacy` as the release mode until those results are retained.

On a Docker-capable development host, run the PostgreSQL-only migration, trigger, and seeded
HNSW planner checks before relying on the Compose stack operationally:

```bash
CLERKSAN_RUN_POSTGRES_TESTS=1 \
  .venv/bin/python -m pytest -q \
  tests/db/test_postgres_schema_runtime.py tests/db/test_postgres_audit_trigger.py
```

The test starts an isolated `pgvector/pgvector` container, applies every migration twice,
checks trigger-owned audit behavior, and proves the seeded ANN query selects the HNSW index.
It is intentionally skipped when Docker is absent.

The API is published only as `127.0.0.1:8000`; PostgreSQL and Ollama do not publish host
ports. The API service applies ordered migrations before starting. Core `/ready` failure is
reserved for unsafe database, schema, or storage state. Missing configured local models set
`processing_ready: false` with a processing reason while core intake and review can remain ready;
jobs that depend on those models stay queued. A vision OCR model is required only when
`CLERKSAN_OCR_ENGINE=vision_llm`; local `paddleocr` or `yomitoku` runtimes do not add an Ollama OCR
model requirement. The worker runs with concurrency 1 by default.

Every request is constrained to the configured loopback host before routing. The receive boundary
also limits size, parts, fields, JSON depth, and concurrent uploads before parsing. By default it
allows 10 seconds for each next body chunk; override only with the positive finite
`CLERKSAN_REQUEST_RECEIVE_TIMEOUT_SECONDS` setting documented in `.env.example`.

When upgrading an existing database, migration `0013_source_bound_format_derivatives.sql`
removes OOXML-derived rows whose original-source version was not retained and queues a
`rebuild_format_derivatives` maintenance job for each affected current source. Keep the worker
running until those jobs finish; this restores spreadsheet/media projections without appending
an extraction or sending a reviewed document back to human review.

The Compose API image builds and serves the React UI from the same loopback origin. Open
`http://127.0.0.1:8000` after the API health check succeeds. There is no separate frontend
server and no permissive browser CORS path. Do not publish the API beyond loopback unless you
have separately designed and reviewed authentication and network exposure.

## Backup and restore

Create a manifest-verified SQLite demo backup:

```bash
.venv/bin/python -m clerksan.tools.backup snapshot-sqlite \
  .clerksan-demo/clerksan.sqlite .clerksan-demo/doc_store .clerksan-backup

.venv/bin/python -m clerksan.tools.backup verify .clerksan-backup
```

Restore replaces the target database and document store, so it requires an explicit
acknowledgement:

```bash
.venv/bin/python -m clerksan.tools.backup restore-sqlite \
  .clerksan-backup .clerksan-demo/clerksan.sqlite .clerksan-demo/doc_store \
  --confirm ERASE_LOCAL_DATA
```

Stop every API and worker process using that SQLite database before running the restore,
then start them again afterwards. Replacing a live SQLite database or its document store
while a process still has it open is unsafe.

For PostgreSQL plus Compose volumes, keep `POSTGRES_PASSWORD` exported in the same terminal
and use the project dependency environment for the manifest step inside the scripts. The backup
briefly quiesces API/worker writes, snapshots PostgreSQL and `doc_store` together, then resumes
only the app services that had been running. If those app containers are intentionally stopped,
the script uses a one-shot API container to stream the document store without starting the API or
worker persistently. Its destination must be a new empty directory:

```bash
backup_dir="$PWD/backups/$(date -u +%Y%m%dT%H%M%SZ)"
bash ./scripts/backup.sh "$backup_dir"

CLERKSAN_RESTORE_CONFIRM=ERASE_LOCAL_DATA \
  bash ./scripts/restore.sh "$backup_dir"
```

Those commands use the default `ordinary` mode: the scripts restore only the API/worker running
state they observed before the operation. For a migration or controlled restore drill, keep the
maintenance fence closed instead:

```bash
CLERKSAN_BACKUP_MODE=maintenance \
  bash ./scripts/backup.sh "$backup_dir"

CLERKSAN_RESTORE_MODE=maintenance \
CLERKSAN_RESTORE_CONFIRM=ERASE_LOCAL_DATA \
  bash ./scripts/restore.sh "$backup_dir"
```

Maintenance backup verifies drained job/worker leases and a clean storage quarantine, records a
sanitized database/store inventory, and leaves API/worker writers stopped. Maintenance restore
requires that inventory, verifies the restored database/store against it, and also leaves the fence
closed for explicit operator checks. Both restore modes are destructive and refuse to run without
the exact `ERASE_LOCAL_DATA` acknowledgement.

After `docker compose down` or a destructive drill, recreate and wait for the data services
before invoking restore:

```bash
docker compose up -d db ollama
```

The restore verifies the manifest before changing anything, quiesces API/worker writes, and
stages the document-store replacement before restoring PostgreSQL. It resumes only services
that were running when restore began, recreating them through Compose if their containers were
removed. If the database restore fails, it attempts to put the previous document store back.
Retain the backup and inspect the command result; perform a real restore drill before relying
on backups operationally.

## Privacy and supply-chain gate

Run the privacy gate only after selecting the exact Git index for release. It scans staged names
and blobs, not unstaged working-tree content. This is a preflight check, not release evidence:

```bash
.venv/bin/python scripts/check-private-artifacts.py --staged
```

After creating the clean-root candidate commit, bind its immutable tree to the reviewed external
manifest and scan the complete tree:

```bash
.venv/bin/python scripts/verify-candidate-manifest.py verify-tree \
  --repository <candidate-repository> \
  --manifest <external-candidate-manifest.json> \
  --tree <root-commit>
.venv/bin/python scripts/check-private-artifacts.py \
  --repository <candidate-repository> \
  --tree <root-commit> \
  --local-pattern-file <absolute-ignored-local-pattern-file>
```

The manifest must stay outside the candidate. An empty staged index, staged-diff-only scan, or
working-tree scan cannot substitute for these immutable-tree checks.

Every staged plan report is blocked until that individual reviewed path is approved with one
`--allow-report <repository-path>` argument. When acceptance sources need additional matching,
optionally pass `--local-pattern-file <absolute-ignored-local-pattern-file>` or set
`CLERKSAN_PRIVATE_PATTERN_FILE`; the file must remain local, ignored, and untracked. Diagnostics are
redacted. Use an absolute pattern-file path when `--repository` targets a different worktree. Never
put the private patterns or source metadata in a tracked config, report, or command line.

Generate the deterministic source dependency inventory with the procedure in
[`sbom/README.md`](sbom/README.md). That artifact covers locked Python and npm sources only. It is
not a built-container or native operating-system SBOM; generate and review those on the
Docker-capable release host before enabling universal intake.

## Tests

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check clerksan tests
```

The `.venv` in these commands is the fresh hash-enforced environment created under
[Locked Python dependencies](#locked-python-dependencies).

Read [docs/compliance_denchoho.md](docs/compliance_denchoho.md) before making any
compliance-related deployment claim.
