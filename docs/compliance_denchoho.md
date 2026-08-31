# 電子帳簿保存法 compliance-supporting matrix

Last source-evidence review: 2026-07-16.

Clerk-san is **compliance-supporting** software. This matrix is a technical evidence map,
not a legal opinion, a certification, or a guarantee that a particular business meets the
電子帳簿保存法. A tax professional and the records-preservation owner must review the deployed
system, retained documents, and operating procedure before making any compliance claim.

## Scope and sources

The National Tax Agency distinguishes electronic books, scanned preservation, and electronic
transaction data. Electronic transaction data includes information exchanged by email,
Internet services, and EDI; its source record must be kept electronically. See the
[NTA overview](https://www.nta.go.jp/law/joho-zeikaishaku/sonota/jirei/02.htm) and the
[Article 10 guidance](https://www.nta.go.jp/law/joho-zeikaishaku/sonota/050228/05.htm).

For the search controls relevant here, NTA guidance describes keys such as date, amount, and
counterparty, date/amount range filters, and combinations of two or more keys. See
[NTA Q&A on search functionality](https://www.nta.go.jp/law/joho-zeikaishaku/sonota/jirei/pdf/0020006-168_03.pdf).

## Evidence boundary

This matrix records delivered repository controls and cited test sources. It is not itself a
release-evidence record for a particular deployment, Docker/Compose run, PostgreSQL
integration, restore drill, or professional review. Capture those dated results in the release
evidence package before they support an operational or compliance-oriented claim.

The universal-intake implementation adds source-intent, immutable mapping, candidate-decision,
and atomic batch-activation evidence, but the release default remains `legacy`. Its local
SQLite/browser evidence does not substitute for the still-open parser-sidecar, PostgreSQL, and
restore gates.

Status labels:

- **Implemented (code/test)** — a repository control and cited automated test source exist.
  This does not assert Docker/Compose, live PostgreSQL, restore, UI, or professional/operating
  validation. Trigger-owned controls must use PostgreSQL migrations rather than the SQLite demo.
- **Partial** — a useful code control exists, but a required runtime, operational, production,
  or professional-review step remains open.
- **Gap** — no safe claim should be made until the listed action is complete.
- **Out of scope** — Clerk-san deliberately does not claim this statutory role.

## Evidence matrix

Each row is self-contained public evidence. Internal plan identifiers are deliberately excluded so
the matrix remains meaningful in a clean source snapshot.

| Requirement area | Preservation category | Control and evidence | API / operating evidence | Automated test source | Status and required action |
|---|---|---|---|---|---|
| Determine which preservation regime applies | Electronic transaction, scanned paper, electronic books | `documents.document_class` records receipt, invoice, recurring bill, quote, or other; it does not itself decide the taxpayer's statutory regime. | Operator must classify the source and retain the original acquisition context. | `tests/db/test_migration_contracts.py::test_core_schema_preserves_three_tiers_and_artifact_versions` | **Partial.** Add a documented classification decision to the operating procedure for each source channel. |
| Preserve original and prove integrity | Electronic transaction; scanned paper | `source_intakes` binds intent and disposition to an exact immutable `document_files` source/version/checksum. PostgreSQL append-only guards prevent update/delete; replacement appends a source version rather than mutating history. Safe but unavailable processing may retain the original as `stored_unprocessed`; unsafe/ambiguous input still fails closed. | Original and exact-source preview/download routes verify source identity and checksums. Raw PDFs are attachment-only; a browser preview requires a complete source-bound inert page manifest. Backup verification covers the database/store inventory before destructive restore. | `tests/api/test_universal_intake.py`; `tests/api/test_ingest_api.py`; `tests/api/test_security_headers.py`; `tests/tools/test_backup.py` | **Partial.** Repository and local SQLite/browser evidence exists. Run the append-only, preview-lineage, and restore paths against PostgreSQL/Compose and retain deployment checksum evidence before relying on them operationally. |
| Prevent undocumented correction/deletion | Electronic transaction; scanned paper; electronic books | `audit_log` is trigger-owned and append-only. Triggers compare actual fields; the actor comes from transaction-local context with a `db:<role>` fallback. Immutable extraction content is superseded instead of overwritten. | `GET /documents/{document_id}` exposes linked audit history. | `tests/db/test_migration_contracts.py::test_audit_is_trigger_owned_append_only_and_has_actor_fallback`; `tests/api/test_review_api.py::test_review_approval_uses_extraction_version_and_conflicts_when_stale`; `tests/db/test_postgres_schema_runtime.py::test_all_migrations_reflect_schema_and_use_hnsw_for_seeded_ann_search`; `tests/db/test_postgres_audit_trigger.py::test_verified_insert_audit_canonicalizes_typed_source_values_before_corrections` | **Partial.** PostgreSQL trigger checks are opt-in and have not been run on this machine. Retain a real trigger result and validate database-role permissions before relying on them in operations. |
| Search by date, amount, and counterparty | Electronic transaction; scanned paper; electronic books | `verified_records` contains all three keys and dedicated indexes. | `GET /documents` accepts `date_from`, `date_to`, `amount_min`, `amount_max`, and `counterparty`. | `tests/db/test_repositories.py::test_combined_verified_range_query` | **Implemented (code/test).** Keep API router registration and UI coverage in the release gate. |
| Range and combined-condition search | Electronic transaction; scanned paper; electronic books | `verified_records_combined_search_idx` supports counterparty plus date/amount paths; repository applies every supplied condition conjunctively. | Same `GET /documents` filters can be combined. | `tests/db/test_repositories.py::test_combined_verified_range_query` | **Implemented (code/test).** Use a production-sized API/UI acceptance fixture before professional review. |
| Link source document to reviewed record | Electronic transaction; scanned paper | `verified_records` has a composite foreign key to the source extraction and document. `recurring_bills` also guards that its document and verified record match. | Document detail returns source file metadata, latest extraction, and verified record together. | `tests/db/test_migration_contracts.py::test_verified_search_and_source_linkage_indexes_are_declared`; `tests/bills/test_service.py` | **Implemented (code/test).** The original file itself must remain accessible to the operator in the deployed system. |
| See and export correction/deletion history | Electronic transaction; scanned paper; electronic books | `audit_log` is readable through `clerksan.db.audit.read_history`; the audit CSV exporter serializes IDs, timestamps, actor, row, field, and old/new values without writing the log. | `GET /documents/{document_id}` shows history. `GET /export/audit?date_from=...&date_to=...` downloads UTF-8 audit evidence. | `tests/export/test_audit_csv.py`; `tests/export/test_export_routes.py` | **Partial.** Exercise the export against PostgreSQL trigger-generated rows before relying on it operationally. |
| Human review before a ledger/export handoff | All source categories | Extracted candidates remain separate from verified records. Complete immutable mapping membership/ignore decisions, per-record lineage, and decision revisions stay source/batch bound; mapping creates no verified row. Singleton approval and multi-record activation are version checked, and verified-only consumers require the active approved financial cohort. | Legacy singleton approval remains compatible. Generic or multi-record mutation uses the React batch review/activation routes; stale source/mapping/batch/vector evidence blocks mutation, and the singleton fallback cannot approve it. | `tests/api/test_review_api.py`; `tests/api/test_mapping_api.py`; `tests/api/test_review_batch_api.py`; `tests/db/test_mapping_repositories.py`; `tests/export/test_accounting_csv.py` | **Partial.** Code/local tests support the review boundary. Accounting outputs still require policy review, and PostgreSQL concurrency/trigger proof remains open. |
| Accounting-export auditability | Electronic transaction; scanned paper | freee and Yayoi outputs include a deterministic order, reviewed counterparty, and Clerk-san record reference; formula-like cells are escaped. | `GET /export?format=freee` emits UTF-8; `format=yayoi` emits Shift_JIS after router mounting. | `tests/export/test_accounting_csv.py` | **Partial.** Confirm account/tax mappings and vendor import behavior in the customer's actual chart of accounts. |
| Readable display / inspection on demand | Electronic transaction; scanned paper | The immutable original remains under the configured document store and is served only when its resolved path stays beneath that store and its SHA-256 checksum matches. | Safe images may render inline. Raw PDFs and office/document originals are attachment-only; PDF inspection uses a complete exact-source inert page manifest or an explicit preview-unavailable result. | `tests/api/test_ingest_api.py`; `tests/api/test_security_headers.py`; `web/src/components/original-preview.test.tsx`; `tests/ui/test_app_contract.py` | **Implemented (code/test).** Operators must include source inspection in their adopted procedure and validate the shipped viewer on the release target. |
| Scanner-preservation-specific controls | Scanned paper | The system can preserve image/PDF source bytes and normalized derivatives, but it does not yet prove scanner-preservation requirements such as capture-process controls, required inspection steps, or timing/quality procedures. | Operator procedure is not yet bundled. | No scanner-specific acceptance suite exists. | **Gap.** Do not describe scanned uploads as compliant scanner preservation until professional review and operating controls are implemented. |
| Backup, restore, retention, and disaster recovery | All retained data | Compose backup fences writers/leases, excludes runtime quarantine, records a sanitized database/store inventory, dumps PostgreSQL and document storage, and writes a checksum manifest. Ordinary mode resumes only previously running writers; maintenance mode keeps the fence closed. Restore verifies manifest/inventory and requires explicit destructive confirmation before replacing targets. | Source intakes, mapping sets, batches/candidates/decisions, active cohorts, preview manifests/pages, verified rows, audit, and original references are covered by the logical inventory. Scheduled frequency, retention, off-machine copy, access ownership, and a completed restore log are not configured by code. | `tests/tools/test_backup.py`; `tests/test_infrastructure.py` | **Partial.** Source/SQLite/script evidence exists, but no PostgreSQL restore drill was completed on this host. Define operations and prove restored originals, lineage, authority, verified records, and audit history on the release target. |
| Operational documentation | All source categories | NTA publishes samples for a correction/deletion prevention procedure and scanner-preservation procedure. | Start from the [NTA sample procedures](https://www.nta.go.jp/law/joho-zeikaishaku/sonota/jirei/0021006-031.htm), adapt them to actual roles, storage, backups, review, and exception handling, then approve and retain the version in use. | No local approved procedure is present. | **Gap.** Create, approve, train against, and periodically review the procedure. |
| Electronic books / 優良な電子帳簿 | Electronic books | Clerk-san preserves source documents and reviewed record projections; it is not a complete statutory bookkeeping or general-ledger system. | Accounting data is exported to the selected accounting system rather than presented as a statutory electronic book. | Not applicable. | **Out of scope.** Do not claim 優良な電子帳簿 status. |
| Product wording and professional sign-off | All source categories | README and runbooks use “compliance-supporting” and local-first wording. React is served on loopback; Streamlit is a loopback singleton fallback. UI copy distinguishes core readiness from processing/model unavailability and distinguishes generic upload from bill scan. | The intake default remains `legacy`. Universal wording must stay gated until sidecar, matching worker lease, legacy-job drain, PostgreSQL, and restore evidence is retained. Review all UI/deployment/export copy against the professional conclusion before release. | `tests/ui/test_translations.py`; `web/src/app.test.tsx`; `web/src/features/intake/intake-view.test.tsx`; documentation review is otherwise manual. | **Partial.** Local-first wording has automated coverage; universal release evidence and professional review remain open. |

## Required runtime and operating gate before a compliance-oriented release

The following gates are not satisfied merely because the matching source code or test source
exists. Retain their actual results with the release evidence.

1. Install from `requirements.lock` with hash enforcement, then record the pinned test/lint results.
   If the local-model demo supports a release claim, retain its synthetic result and model versions.
2. Keep the deployment in `legacy` until universal sidecar probes, a fresh matching API/worker
   capability lease, and the legacy-job drain are all recorded on the target.
3. Apply migrations to PostgreSQL and record the migration checksums.
4. Confirm original files, exact-source preview evidence, mapping/batch lineage, lifecycle history,
   atomic activation, and audit entries with a real database role.
5. Verify date, amount, counterparty, range, and combined searches through the shipped API/UI.
6. Test the shipped bills/export routers, including audit-history CSV download.
7. Create a maintenance-fenced PostgreSQL backup, restore it into an isolated environment, and
   compare original checksums, mappings, active cohorts, verified records, recurring-bill links,
   preview artifacts, and audit history.
8. Run the staged privacy gate with explicit report approvals. Generate/review a built-image and
   native-package SBOM; the checked-in source dependency SBOM is insufficient for that claim.
9. Adopt the NTA-derived operating procedure, set retention/backup ownership, and obtain
   professional review. If any step fails, keep the product wording at “compliance-supporting”.
