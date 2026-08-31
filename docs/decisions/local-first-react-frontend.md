# ADR: Local-first React frontend

**Status:** Accepted, 2026-08-17

## Decision

Clerk-san will use a React, TypeScript, and Vite workspace in `web/` for the browser
presentation. FastAPI remains the only browser-to-data boundary. Production serves Vite's
static build from `http://127.0.0.1:8000`; development uses a loopback Vite proxy.

## Context

The current Streamlit entry point contains scan, review, history, search, and bills screens,
but the review workspace needs independent source evidence, form edits, duplicate cues, clear
version state, and keyboard actions. Those are natural component boundaries rather than a
single rerun-oriented dashboard surface.

The document pipeline is intentionally unchanged: FastAPI persists requests, the worker owns
OCR and extraction, PostgreSQL owns durable audit behavior, and Ollama remains local. React
must not call the database, document store, worker, Ollama, or a cloud service directly.

## Consequences

- React/Vite has the highest review-UX ceiling while retaining a static, local deployment.
- The built UI becomes part of the API image; Vite is never a production service.
- Browser mutations validate exact loopback Origin and Host values. There is no wildcard CORS.
- The top-level intake actions are distinct intents: **Upload file** starts the generic/mapping
  workflow, while **Scan bill** accepts one receipt/invoice image or PDF.
- Generic tabular candidates remain staged through mapping and per-record review; one atomic
  activation makes the approved cohort authoritative. React never treats staging as verification.
- `translations.py` is canonical while Streamlit remains a rollback path. A checked generator
  produces the frontend catalog and rejects locale/key drift.
- Review responses bind an extraction to `source_file_id` and `source_version`; original
  inspection can request that exact immutable source instead of silently using the newest file.
- Images may be displayed inline. Raw PDFs and office/document originals are attachment-only;
  PDF viewing uses a complete exact-source manifest of inert PNG pages or a typed
  preview-unavailable state.
- Missing models are presented as processing unavailability while core safe intake/review can
  remain available; the UI does not collapse those states into one outage message.

## Universal-intake boundary

React being the default UI does not make universal processing the default. The static runtime mode
remains `legacy`, with an empty universal process advertisement. Universal UI outcomes may be used
only after the parser sidecar probes succeed, the API sees a fresh matching worker capability
lease, and queued/running legacy-profile jobs are drained. Loss of that evidence withdraws process
capabilities and never authorizes an in-process fallback.

The current rollout remains blocked on Docker/sidecar, PostgreSQL, and restore evidence. Until those
external gates close, universal behavior is an operator-gated implementation rather than the
release baseline.

## Alternatives considered

| Option | Decision | Reason |
| --- | --- | --- |
| Streamlit plus CSS | Keep as fallback | Fast to retain, but the evidence-aware review layout has a practical interaction ceiling. |
| NiceGUI | Reject | It adds a framework without materially improving the frontend component ecosystem. |
| React + TypeScript + Vite | Adopt | Static local deployment, mature accessible primitives, and the necessary UX headroom. |
| React + Tauri | Defer | Native packaging is a distribution choice, not a prerequisite for a better local web UI. |
| Next.js | Reject | SSR and an extra application server add no value for a loopback-only static application. |

## Non-goals

No accounts, remote hosting, Tauri packaging, cloud OCR, database access from the browser, or
browser export UI are introduced by this decision.

## Cutover rule

Streamlit stays runnable until React passes the complete safety walkthrough and reduces the
same reviewer's median completion time by at least 20% across the declared five-fixture,
three-run, counterbalanced benchmark. An immutable Streamlit rollback artifact and smoke test
are required before deletion.
