# Clerk-san developer preview

Clerk-san is a local-only developer preview for reviewing receipts and invoices. The supported
path uses host Ollama, SQLite, the loopback API and worker, and the built React interface. A human
must approve extracted fields before Clerk-san treats them as verified records.

This preview is not production-ready, multi-user, internet-facing, legally certified, or a proof
that every advanced parser and PostgreSQL control works on your machine.

## Supported first run

Prerequisites:

- Python 3.11 and `uv`;
- Node 24 LTS with npm 11;
- Ollama running on `127.0.0.1:11434`; and
- the following models pulled explicitly by the operator.

```bash
ollama pull gemma3:4b
ollama pull qwen2.5:7b
ollama pull nomic-embed-text:v1.5

uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python --require-hashes --no-deps \
  --requirement requirements.lock

scripts/local-app.sh init-demo
scripts/local-app.sh start
```

Open `http://127.0.0.1:8000`. The launcher builds the locked React source, uses legacy intake,
runs the API and worker with demo mode disabled, and never starts Ollama or downloads a model.
`init-demo` accepts only an absent or empty destination and creates deterministic synthetic data.

For later sessions:

```bash
scripts/local-app.sh start
scripts/local-app.sh status
scripts/local-app.sh stop
```

## Readiness is split intentionally

`/ready` and the launcher report two different states:

- **core readiness**: the database, schema, and storage are safe for loopback intake and review;
- **processing readiness**: required model tags, the configured embedding digest, and a fresh
  matching worker capability lease are also available.

A missing model can leave core operations available while processing-dependent jobs remain queued.
An HTTP 200 response alone is not evidence that document processing is ready.

Launcher diagnostics retain only stable, non-sensitive reason codes. In particular,
`local_data_needs_upgrade` means the SQLite schema needs operator attention before intake;
`required_model_missing` means at least one configured tag is absent; and
`embedding_digest_mismatch` means the installed embedding model does not match the configured
integrity pin. Run `scripts/local-app.sh status` for the redacted state and inspect `/ready` locally
when more detail is needed.

The following model IDs were observed on the maintainer's validation host for this source preview:

| Model | Observed manifest digest | Enforcement |
| --- | --- | --- |
| `gemma3:4b` | `a2af6cc3eb7fa8be8504abaf9b04e88f17a119ec3f04a3addf55f92841195f5a` | Availability by mutable tag |
| `qwen2.5:7b` | `845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e` | Availability by mutable tag |
| `nomic-embed-text:v1.5` | `0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f` | Exact digest enforced |

The first two rows are observations, not integrity pins. Pulling the same tag later may produce
different bytes and requires a new synthetic validation. Model weights stay outside this repository
and remain under their own terms.

## Local data and rollback

The default demo stores its SQLite database and document store under `.clerksan-demo/`. Launcher
process records, logs, and compatibility-upgrade backups stay under `.clerksan-runtime/`. These
ignored directories can contain document data and must never be committed or attached to a public
report.

Before opening an existing SQLite database that may need a compatibility upgrade, the launcher
creates and verifies a rollback snapshot under
`.clerksan-runtime/sqlite-upgrade/pre-upgrade-backups/`. Keep that snapshot until the upgraded data
has been reviewed. Follow [RUN_AND_DEMO.md](../RUN_AND_DEMO.md#backup-and-restore) for manual backup
and destructive restore procedures.

## Advanced Compose path

`docker compose` is optional. It runs separate PostgreSQL and Ollama services and keeps their data
in Compose-owned volumes; the application image does not contain Ollama or model weights. This path
is useful for development against the advanced architecture, but it is not the first-run workflow.

Universal intake remains operator-gated. A source test pass or healthy SQLite run does not establish
the parser sandbox, PostgreSQL audit triggers, restore behavior, built-image supply chain, retention
procedure, or production readiness. See [RUN_AND_DEMO.md](../RUN_AND_DEMO.md) for the guarded Compose
procedure and current open gates.

## Source-preview verification

Run the source checks from a clean copy:

```bash
.venv/bin/ruff check clerksan tests eval scripts
.venv/bin/ruff format --check clerksan tests eval scripts
.venv/bin/python -m pytest -q

cd web
npm ci --ignore-scripts
npm audit --audit-level=high
npm test
npm run typecheck
npm run build
cd ..

SOURCE_DATE_EPOCH=0 scripts/generate-sbom.sh /tmp/clerksan-source.spdx.json
cmp sbom/source-dependencies.spdx.json /tmp/clerksan-source.spdx.json
.venv/bin/python scripts/check-license-policy.py
scripts/run-gitleaks.sh self-test
scripts/run-gitleaks.sh dir .
scripts/run-osv-scanner.sh sbom/source-dependencies.spdx.json
```

These checks cover the source candidate and lockfiles only. They do not clear container images,
native packages, model redistribution, external services, or legal/compliance obligations.

## Maintainer publication checklist

Publication uses a fresh Git root and an external canonical manifest. The candidate must contain
only reviewed source files; never copy the old `.git` directory, plans, reports, local configuration,
runtime data, generated builds, personal documents, or private-pattern files.

Before any visibility change, record and verify:

- target repository owner and name;
- source revision and external manifest SHA-256;
- exactly one new root commit and its tree ID;
- manifest-to-tree equality;
- full immutable-tree privacy scan plus the ignored maintainer-local protected-pattern scan;
- generic secret scan, dependency policy, vulnerability scan, backend/frontend checks, and SBOM
  reproducibility; and
- synthetic host-Ollama evidence with model name/digest observations and separate core/processing
  readiness.

Create and verify the remote privately first. Public visibility is a separate maintainer decision;
no tag, package, image, model volume, registry artifact, or GitHub Release is implied.

## Security and compliance boundary

The API has no authentication and supports loopback/private-network use only. Never expose it on a
public interface. Report vulnerabilities through the private route in [SECURITY.md](../SECURITY.md)
and use synthetic evidence only.

Clerk-san is compliance-supporting software, not legal advice, a certification, or a guarantee of
compliance. Review the [electronic-bookkeeping evidence matrix](compliance_denchoho.md) before making
an operational claim.
