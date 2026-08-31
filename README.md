# Clerk-san

Clerk-san is a **local-only, legacy-intake developer preview** for reviewing receipts and invoices.
The supported first-run path uses host Ollama, SQLite, the loopback launcher, and the built React UI.
It preserves an original, performs local OCR and structured extraction, requires human approval
before a record becomes verified, and keeps searchable history, duplicate evidence, backups, and
accounting exports.

Clerk-san does not send documents to a cloud OCR provider. OCR, extraction, routing,
and retrieval use the configured local models.

> Clerk-san is **電子帳簿保存法 compliance-supporting**, not a legal certification or a
> guarantee of compliance. See [the evidence matrix](docs/compliance_denchoho.md).

## Quick local setup and recurring start

Install [Ollama](https://ollama.com/), [uv](https://docs.astral.sh/uv/), and Node 24 LTS with npm 11,
then pull the three local models:

```bash
ollama pull gemma3:4b
ollama pull qwen2.5:7b
ollama pull nomic-embed-text:v1.5

scripts/local-app.sh init-demo
scripts/local-app.sh start
```

`init-demo` creates a non-personal synthetic receipt and runs the real local path once. It
only accepts an absent or empty destination and never resets existing data. `start` performs a
locked Vite build, prepares a verified rollback snapshot before a possible SQLite schema upgrade,
then runs the loopback API and worker with demo mode disabled. Open the React UI at
`http://127.0.0.1:8000`.

The local path is:

```text
original → OCR → classification → extraction → review → verified record → search
```

The React intake screen has two explicit choices: **Upload file** for generic information
files (including CSV/XLSX when universal intake is active) and **Scan bill** for one receipt
or invoice image/PDF. The host launcher intentionally stays in legacy mode because it cannot
prove the container sandbox; use the isolated Compose procedure in `RUN_AND_DEMO.md` for broad
file intake.

React is the default local UI, while the intake-processing default remains
`CLERKSAN_INTAKE_MODE=legacy`. Universal processing is operator-gated: the parser sidecar probes,
a fresh matching API/worker capability lease, and the legacy-job drain must all pass. It is still
release-blocked until Docker/Compose, PostgreSQL, and restore evidence is completed on a capable
host. Missing local models or an embedding-digest mismatch affect processing readiness and keep
dependent jobs queued; they do not by themselves make safe core intake and review unavailable.
`start` and `status` parse `/ready` and report core readiness separately from processing readiness.
The launcher never starts Ollama or downloads models.

Use `scripts/local-app.sh status` to diagnose local prerequisites and
`scripts/local-app.sh stop` when finished. For production-like PostgreSQL setup, the
React UI, backups, and troubleshooting, read
[RUN_AND_DEMO.md](RUN_AND_DEMO.md). The Compose procedure requires a local
`POSTGRES_PASSWORD`; keep that password out of version control.

### Retained Streamlit fallback

React is the default local UI. Streamlit remains runnable as a manual fallback while the
[cutover rule](docs/decisions/local-first-react-frontend.md#cutover-rule) is still pending.
To inspect it against the already-running local API, run this in another terminal and open
`http://127.0.0.1:8501`:

```bash
CLERKSAN_API_URL=http://127.0.0.1:8000 \
  .venv/bin/python -m streamlit run app.py \
  --server.address 127.0.0.1 --server.port 8501
```

Stop it with `Ctrl-C`. This manual local server is not a Compose/image rollback and does not
satisfy the hard cutover gate: retain Streamlit until the 20% benchmark and immutable container
rollback checks in the decision record are complete.

## Preview boundary

The supported developer preview includes the loopback FastAPI service, built React UI, separate
worker, SQLite storage, legacy receipt/invoice intake, explicit human review, verified history,
local search, recurring-bill analysis, backups, and verified-only exports.

The source tree also contains advanced Compose/PostgreSQL and universal-intake work. Those paths are
operator-gated and remain unreleased: source code and SQLite checks do not prove the parser sandbox,
PostgreSQL audit behavior, restore procedure, image supply chain, or production readiness. Compose is
an opt-in advanced runtime with its own PostgreSQL and Ollama volumes; it is not the quick-start path.

Read the one-page [developer preview guide](docs/developer-preview.md) for prerequisites, readiness,
data locations, the model evidence boundary, source checks, and the maintainer publication checklist.
User-visible changes are summarized in the [changelog](CHANGELOG.md).

## Validation

`requirements.lock` is the runnable dependency authority. For a clean, hash-enforced install,
create a fresh environment and install every transitive entry without dependency re-resolution:

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python --require-hashes --no-deps -r requirements.lock
.venv/bin/python -m pytest -q
.venv/bin/ruff check clerksan tests
```

`requirements.txt` is the human-maintained source input used only when intentionally regenerating
and reviewing `requirements.lock`; routine installs, runs, and tests use the lock.

The PostgreSQL trigger and Docker lifecycle checks require Docker Desktop or another
Docker-compatible runtime. The repository’s local SQLite demo intentionally does not
claim those PostgreSQL-only guarantees.

The checked-in [source dependency SBOM](sbom/README.md) inventories the Python and npm lockfiles.
It is not a built-image or native operating-system SBOM. After selecting the exact release index,
run the staged privacy preflight described in
[RUN_AND_DEMO.md](RUN_AND_DEMO.md#privacy-and-supply-chain-gate). A staged-only or empty-index pass is
not release evidence; the external candidate manifest, immutable root tree, and full-tree privacy
scan remain mandatory before publication.

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing a change. Report vulnerabilities only through
the private route in [SECURITY.md](SECURITY.md); never attach real receipts, invoices, credentials, or
private document data to a pull request or public discussion.

## License

[MIT License](LICENSE), Copyright (c) 2026 PHAM BAO NAM. Dependencies, model weights, base images,
and operating-system packages remain under their own terms.
