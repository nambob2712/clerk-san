# Clerk-san final setup and run guide

This is the short operator guide for the current repository state. It covers the verified
no-Docker path and points to the longer runbook for gated or destructive operations.

## Verification status

The checked-in source tests cover the hash-locked no-Docker launcher, loopback React UI, request
boundary checks, synthetic browser workflow, source-dependency SBOM generation, and privacy-gate
behavior. Reproduce the current gates from the [README validation section](README.md#validation)
and use [RUN_AND_DEMO.md](RUN_AND_DEMO.md) for the supported local workflow.

The release remains blocked on a Docker-capable host for the container build and parser-sidecar
probes, live PostgreSQL migration/audit/concurrency checks, and a PostgreSQL backup/restore drill.
A built-image/native SBOM, operating-procedure ownership, retention proof, and professional review
also remain open. Do not describe universal processing or legal compliance as released.

## No-Docker setup and recurring start

This is the normal path. It keeps documents local and uses the already-pulled local models.
Run it from the repository root. Pull the models once, then create a clean hash-enforced Python
environment from the committed lock:

```bash
ollama pull gemma3:4b
ollama pull qwen2.5:7b
ollama pull nomic-embed-text:v1.5

uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python --require-hashes --no-deps -r requirements.lock

scripts/local-app.sh init-demo
```

`requirements.lock` is the runnable dependency authority. `requirements.txt` is only the
human-maintained source input for an intentional, reviewed lock regeneration.

The demo follows the real local workflow:

```text
preserve original → local OCR → local extraction → pending review
→ approve → verified record → local semantic search
```

`init-demo` only accepts an absent or empty data directory and never replaces existing demo
data. Inspect the result with:

```bash
.venv/bin/python -m json.tool .clerksan-demo/demo-result.json
```

## Open and stop the UI

Start the entire local UI stack with one command:

```bash
scripts/local-app.sh start
```

Open `http://127.0.0.1:8000`. Check or stop only the launcher-owned API, worker, and UI with:

```bash
scripts/local-app.sh status
scripts/local-app.sh stop
```

To open an existing non-default synthetic dataset without changing source code, use:

```bash
scripts/local-app.sh start --data-dir .clerksan-existing-demo
```

The launcher never resets data, never starts/stops or downloads models for host Ollama, and refuses
unknown port occupants or unowned PID records. It always uses the default `legacy` intake mode because the host
process cannot prove the parser sandbox. The React UI still presents two explicit intents:
**Upload file** for generic information and **Scan bill** for one receipt/invoice image or PDF.
Keep the terminal window open while testing; when finished, run `scripts/local-app.sh stop`.

Normal startup explicitly uses `CLERKSAN_DEMO_MODE=false`. Core readiness covers safe database,
schema, and storage state. Processing readiness additionally requires the configured model tags,
the pinned embedding digest, and a fresh matching worker capability lease. If one is unavailable,
`processing_ready` becomes false and dependent jobs remain queued; the launcher reports that
degraded state without mislabeling it as full processing readiness.

Before the API can apply a SQLite compatibility upgrade, the launcher creates and verifies a local
rollback snapshot under `.clerksan-runtime/sqlite-upgrade/pre-upgrade-backups/`. It records the
post-start schema only after normal-mode core readiness succeeds. Keep this ignored runtime directory
until the upgraded data has been checked; it can contain a copy of local document data.

## Interface languages

Use the visible **Language** control in the sidebar to switch the active UI between English,
Tiếng Việt, and 日本語. This changes Clerk-san presentation copy only. Original documents,
filenames, extraction/audit values, API payloads, and backend-generated query answers stay
exactly as received.

## Full Docker stack in legacy mode

Use this only on a Docker-capable host with a local `POSTGRES_PASSWORD`.

```bash
export POSTGRES_PASSWORD='choose-a-long-local-password'
docker compose up -d db ollama
docker compose exec ollama ollama pull gemma3:4b
docker compose exec ollama ollama pull qwen2.5:7b
docker compose exec ollama ollama pull nomic-embed-text:v1.5
docker compose --profile app up -d --build
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/ready
curl --fail http://127.0.0.1:8000/capabilities
docker compose --profile app ps
```

The API and React UI share `http://127.0.0.1:8000`; PostgreSQL and Ollama remain private Compose
services. This command keeps `CLERKSAN_INTAKE_MODE=legacy`. Universal mode requires successful
sidecar probes, a fresh matching API/worker capability lease, and a fully drained legacy job queue,
in addition to the open Docker/PostgreSQL/restore release evidence. Follow the gated procedure in
[RUN_AND_DEMO.md](RUN_AND_DEMO.md#full-local-stack-with-postgresql); do not infer success from these
commands or from SQLite.

## Validate and back up

Validation on this repo:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check clerksan tests eval scripts
git diff --check
```

SQLite demo backup and manifest verification:

```bash
.venv/bin/python -m clerksan.tools.backup snapshot-sqlite \
  .clerksan-demo/clerksan.sqlite .clerksan-demo/doc_store .clerksan-backup

.venv/bin/python -m clerksan.tools.backup verify .clerksan-backup
```

Restore is destructive and requires every API/worker process using the target data to be
stopped first, plus explicit confirmation. Use the detailed procedure in
[RUN_AND_DEMO.md](RUN_AND_DEMO.md#backup-and-restore) instead of improvising the restore command.
Compose backup/restore supports `ordinary` mode, which resumes only previously running writers,
and `maintenance` mode, which verifies the fenced inventory and leaves writers stopped. Every
restore requires the exact destructive acknowledgement.

## Release privacy and supply chain

After selecting the exact staged release files, run:

```bash
.venv/bin/python scripts/check-private-artifacts.py --staged
```

That staged scan is preflight only. Release evidence requires the reviewed external manifest to
match the clean root commit and a full-tree privacy scan:

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

Approve each reviewed staged report explicitly with `--allow-report <repository-path>`. An optional
`--local-pattern-file <absolute-ignored-local-pattern-file>` may add private-source matching, but
that file must remain ignored and untracked and its contents must never enter a report or command.
Use an absolute pattern-file path when `--repository` targets a different worktree. The
checked-in [source dependency SBOM](sbom/README.md) covers locked Python/npm sources only; it is not
a built-image or native operating-system SBOM.

## Important boundaries

- OCR, extraction, routing, and retrieval run through the configured local models.
- PaddleOCR and YomiToku are optional local Python OCR engines. They require separately
  supported local runtimes and are not the demonstrated path in this guide.
- Request-body receive waits default to 10 seconds per next chunk through
  `CLERKSAN_REQUEST_RECEIVE_TIMEOUT_SECONDS`; see `.env.example` before changing the boundary.
- The compliance wording is support-only. See
  [docs/compliance_denchoho.md](docs/compliance_denchoho.md) for the evidence matrix.
- For full troubleshooting, operator notes, and the longer backup/restore procedure, read
  [RUN_AND_DEMO.md](RUN_AND_DEMO.md).
