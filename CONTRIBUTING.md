# Contributing to Clerk-san

Thank you for helping improve Clerk-san. This repository is a source-only, local-first developer
preview. The supported path uses host Ollama, SQLite, the loopback launcher, legacy intake, and human
review before extracted data becomes verified. It is not a production service or a legal or
regulatory compliance certification.

## Before you contribute

Read the [README](README.md), this guide, and the [security policy](SECURITY.md). Keep changes small,
focused, and consistent with the existing public contracts. GitHub Issues are intentionally disabled
for the initial preview; submit concrete changes as pull requests. A small draft pull request may be
used to explain a scoped proposal before its implementation is complete.

Do not use a pull request to disclose a vulnerability. Use GitHub private vulnerability reporting as
described in the security policy.

## Protect document data

Never upload, commit, paste, or link to real receipts, invoices, personal documents, customer data,
database files, runtime storage, local logs, credentials, tokens, or model input/output containing
private information. This applies to pull requests, screenshots, test failures, commit history, and
all other project discussions.

Use minimal, deterministic synthetic fixtures with fictional people and businesses. Remove metadata
and verify that a fixture cannot be mistaken for a real document before submitting it. A redacted real
document is not an acceptable test fixture.

## Preserve the preview boundary

Changes must preserve local processing, loopback-only publication, explicit human verification, and
verified-only downstream records unless the maintainer has approved a documented contract change.
Do not describe SQLite checks as PostgreSQL evidence, source checks as container or deployment proof,
or compliance-supporting behavior as production readiness, legal advice, certification, or a
guarantee of compliance.

Do not commit model weights, built container images, or copied/generated assets without established
redistribution rights and required attribution. Unless a file states otherwise, Clerk-san project code
is licensed under the [MIT License](LICENSE). Dependencies, model weights, base images,
operating-system packages, and other third-party material remain under their own licenses. Preserve
their license and notice files. Update the relevant lockfile and the
[source dependency SBOM](sbom/README.md) when dependencies change.

## Validate the change

Use the locked Python setup and backend checks in the [README validation section](README.md#validation).
For frontend changes, use the Node and npm versions declared in `web/package.json`, then run:

```bash
cd web
npm ci
npm test
npm run typecheck
npm run build
```

Run the narrowest relevant tests first, then the broader gates affected by the change. Do not weaken,
skip, or hide a failing check. Tests and examples must use only synthetic public-safe data and must not
download Ollama models or contact a cloud document-processing service unless a separately approved
workflow explicitly owns that behavior.

## Pull request checklist

In the pull request description:

- explain the user-visible outcome and the reason for the change;
- identify the focused tests and checks you ran;
- state whether behavior, setup, security, dependencies, or public documentation changed;
- confirm that the change and its history contain no personal documents, private data, or secrets;
- confirm that new material may be distributed with the repository and retains any required third-party
  attribution; and
- avoid unsupported production, security, performance, or compliance claims.

By submitting a contribution, you confirm that you have the right to submit it and agree that accepted
contributions may be distributed under this repository's MIT License. Do not submit material whose
terms are incompatible with that distribution.
