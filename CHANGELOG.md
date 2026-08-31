# Changelog

All notable user-visible changes to Clerk-san are recorded here. This project does not currently
publish version tags or GitHub Releases.

## Unreleased developer preview

- Added the local-only host-Ollama, SQLite, API/worker, and React first-run workflow.
- Added explicit human review before extracted data becomes a verified record.
- Added truthful core-versus-processing readiness and pre-upgrade SQLite rollback snapshots.
- Added the original Clerk Pivot mark, responsive lockups, and product icon set.
- Added locked dependency, source-SBOM, privacy, secret, license, and vulnerability verification.
- Raised Pillow and pytest-family locks to patched releases after an OSV review, without changing
  the remaining Python package versions.
- Kept Compose/PostgreSQL and universal intake as advanced, gated source paths rather than released
  capabilities.

The public snapshot, if approved, is a source-only developer preview. It does not include model
weights, container images, packages, tags, or a GitHub Release.
