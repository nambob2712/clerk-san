import { expect, test, type Locator, type Page, type Request } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

import {
  expectNoUnexpectedTraffic,
  expectViewportContained,
  installLocalApiMock,
  multipartText,
  requestJson,
  type MockResponse,
} from "./mock-local-api";

const SHA_SOURCE = "a".repeat(64);
const SHA_NORMALIZED = "b".repeat(64);
const SHA_STRUCTURE = "c".repeat(64);
const SHA_VECTOR = "d".repeat(64);
const SHA_REFRESHED_VECTOR = "9".repeat(64);
const SHA_PAGE_ONE = "e".repeat(64);
const SHA_PAGE_TWO = "f".repeat(64);
const CREATED_AT = "2026-08-23T00:00:00Z";
const PNG_1X1 = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

interface SourceOptions {
  documentId: string;
  intakeId: string;
  sourceFileId: string;
  sourceVersion?: number;
  sourceSha256?: string;
  intakeIntent?: "generic_file" | "bill_scan";
  state?: "queued" | "processing" | "processed" | "needs_mapping" | "stored_unprocessed" | "failed";
  version?: number;
  uploadKey?: string;
}

function sourceIntake(options: SourceOptions): Record<string, unknown> {
  return {
    intake_id: options.intakeId,
    document_id: options.documentId,
    source_file_id: options.sourceFileId,
    source_version: options.sourceVersion ?? 1,
    source_sha256: options.sourceSha256 ?? SHA_SOURCE,
    upload_idempotency_key: options.uploadKey ?? `upload-${options.intakeId}`,
    intake_intent: options.intakeIntent ?? "generic_file",
    state: options.state ?? "processed",
    reason_code: null,
    retryable: false,
    failure_phase: null,
    version: options.version ?? 1,
    job_reference: null,
  };
}

function documentRecord({
  documentId,
  sourceFileId,
  sourceVersion = 1,
  sourceSha256 = SHA_SOURCE,
  filename = "source.csv",
  mime = "text/csv",
}: {
  documentId: string;
  sourceFileId: string;
  sourceVersion?: number;
  sourceSha256?: string;
  filename?: string;
  mime?: string;
}): Record<string, unknown> {
  return {
    id: documentId,
    doc_class: "tabular",
    status: "in_review",
    source_filename: filename,
    created_at: CREATED_AT,
    updated_at: CREATED_AT,
    files: [{
      id: sourceFileId,
      kind: "original",
      version: sourceVersion,
      source_filename: filename,
      mime,
      sha256: sourceSha256,
    }],
    extracted: null,
    verified: null,
    processing_error: null,
    audit_history: [],
  };
}

function batchSummary({
  batchId,
  documentId,
  intakeId,
  sourceFileId,
  candidateCount,
  sourceVersion = 1,
  batchVersion = 1,
  pendingCount = candidateCount,
  includedCount = 0,
  excludedCount = 0,
  lifecycle = "open",
}: {
  batchId: string;
  documentId: string;
  intakeId: string;
  sourceFileId: string;
  candidateCount: number;
  sourceVersion?: number;
  batchVersion?: number;
  pendingCount?: number;
  includedCount?: number;
  excludedCount?: number;
  lifecycle?: "open" | "ready_to_activate" | "active";
}): Record<string, unknown> {
  return {
    id: batchId,
    document_id: documentId,
    source_intake_id: intakeId,
    source_file_id: sourceFileId,
    source_version: sourceVersion,
    lifecycle,
    version: batchVersion,
    candidate_count: candidateCount,
    pending_count: pendingCount,
    included_count: includedCount,
    excluded_count: excludedCount,
    error_count: 0,
    exception_count: 0,
    reconciliation_counts: { pending: pendingCount, included: includedCount, excluded: excludedCount },
    reconciliation_digest: batchVersion.toString(16).padStart(64, "0"),
    created_at: CREATED_AT,
    updated_at: CREATED_AT,
  };
}

function candidate({
  batchId,
  ordinal,
  version = 1,
  latestDecision = null,
}: {
  batchId: string;
  ordinal: number;
  version?: number;
  latestDecision?: Record<string, unknown> | null;
}): Record<string, unknown> {
  return {
    extraction_id: `extraction-${ordinal}`,
    batch_id: batchId,
    candidate_ordinal: ordinal,
    candidate_key: ordinal.toString(16).padStart(64, "0"),
    row_fingerprint: (ordinal + 10_000).toString(16).padStart(64, "0"),
    record_kind: "financial",
    financial_subtype: "transaction",
    source_locator: `transactions!row=${ordinal + 1}`,
    version,
    status: "pending_review",
    payload: { transaction_date: "2026-08-23", total_amount: ordinal * 100, counterparty: `Vendor ${ordinal}` },
    field_confidences: {},
    source_spans: {},
    validation_issues: [],
    evidence_group_keys: [],
    latest_decision: latestDecision,
    duplicate_evidence: [],
  };
}

function fileButton(page: Page, name: string) {
  return page.locator("button").filter({ hasText: name }).first();
}

async function expectMinimumTarget(locator: Locator, minimum = 44): Promise<void> {
  const box = await locator.boundingBox();
  expect(box, "Expected the interactive control to have a rendered box.").not.toBeNull();
  expect(box?.width ?? 0).toBeGreaterThanOrEqual(minimum);
  expect(box?.height ?? 0).toBeGreaterThanOrEqual(minimum);
}

async function tabTo(page: Page, locator: Locator, maximumTabs = 160): Promise<void> {
  for (let index = 0; index < maximumTabs; index += 1) {
    if (await locator.evaluate((element) => element === document.activeElement)) return;
    await page.keyboard.press("Tab");
  }
  throw new Error(`Keyboard focus did not reach ${await locator.getAttribute("aria-label") ?? await locator.textContent() ?? "target"}.`);
}

async function expectNoSeriousAccessibilityViolations(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page }).analyze();
  const blocking = results.violations.filter((violation) => violation.impact === "serious" || violation.impact === "critical");
  expect(blocking, blocking.map((violation) => `${violation.id}: ${violation.help}`).join("\n")).toEqual([]);
}

test("keyboard browse keeps Upload file and Scan bill intents distinct for mixed CSV/XLSX", async ({ page }) => {
  const uploads: Array<{ filename: string; intent: string; idempotencyKey: string }> = [];
  const intakes = new Map<string, Record<string, unknown>>();
  const audit = await installLocalApiMock(page, (request, url): MockResponse | undefined => {
    if (request.method() === "POST" && url.pathname === "/documents") {
      const body = multipartText(request);
      const filename = body.match(/filename="([^"]+)"/u)?.[1];
      const intent = body.match(/name="intake_intent"\r?\n\r?\n([^\r\n]+)/u)?.[1];
      const idempotencyKey = request.headers()["idempotency-key"];
      if (!filename || !intent || !idempotencyKey) throw new Error("Upload omitted filename, intent, or Idempotency-Key.");
      uploads.push({ filename, intent, idempotencyKey });
      const slug = filename.replace(/[^a-z0-9]+/giu, "-").replace(/^-|-$/gu, "").toLowerCase();
      const intakeId = `intake-${slug}`;
      const documentId = `document-${slug}`;
      const sourceFileId = `source-${slug}`;
      intakes.set(intakeId, sourceIntake({
        documentId,
        intakeId,
        sourceFileId,
        intakeIntent: intent as "generic_file" | "bill_scan",
        state: intent === "generic_file" ? "needs_mapping" : "processed",
        uploadKey: idempotencyKey,
      }));
      return {
        json: {
          document_id: documentId,
          status: "uploaded",
          duplicate_of: null,
          source_file_id: sourceFileId,
          source_intake_id: intakeId,
          job_id: `job-${slug}`,
          reason_code: null,
          retryable: false,
        },
      };
    }
    if (request.method() === "GET" && url.pathname.startsWith("/intakes/")) {
      const detail = intakes.get(decodeURIComponent(url.pathname.slice("/intakes/".length)));
      if (!detail) throw new Error(`Unknown intake detail request: ${url.pathname}`);
      return { json: detail };
    }
    return undefined;
  });

  await page.goto("/#intake");
  await expect(page.getByRole("heading", { name: "Add documents" })).toBeVisible();
  await expect(page.getByText(/including CSV or XLSX/u)).toBeVisible();
  await expect(page.getByText(/Broad file processing is not ready/u)).toHaveCount(0);
  await expectNoSeriousAccessibilityViolations(page);
  const workspaceNavigation = page.getByRole("navigation", { name: "Workspace sections" });
  for (const name of ["Intake", "Review", "Documents", "Bills", "Search"]) {
    const navigationButton = workspaceNavigation.getByRole("button", { name, exact: true });
    await expect(navigationButton).toBeVisible();
    await expect(navigationButton).toHaveAccessibleName(name);
    if (page.viewportSize()?.width === 320) await expectMinimumTarget(navigationButton);
  }

  const genericInput = page.locator('input[type="file"][aria-label="Upload file"]');
  const billInput = page.locator('input[type="file"][aria-label="Scan bill"]');
  await expect(genericInput).toHaveAttribute("multiple", "");
  await expect(genericInput).not.toHaveAttribute("accept", /.+/u);
  await expect(billInput).not.toHaveAttribute("multiple", "");
  await expect(billInput).toHaveAttribute("accept", /application\/pdf/u);

  const uploadButton = fileButton(page, "Upload file");
  if (page.viewportSize()?.width === 320) await expectMinimumTarget(uploadButton);
  await uploadButton.focus();
  await expect(uploadButton).toBeFocused();
  const genericChooserPromise = page.waitForEvent("filechooser");
  await page.keyboard.press("Enter");
  const genericChooser = await genericChooserPromise;
  expect(genericChooser.isMultiple()).toBe(true);
  await genericChooser.setFiles([
    { name: "mixed-ledger.csv", mimeType: "text/csv", buffer: Buffer.from("date,amount\n2026-08-23,100\n") },
    { name: "mixed-ledger.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: Buffer.from("synthetic-xlsx-e2e") },
  ]);
  await expect(page.getByText("Mapping required")).toHaveCount(2);

  const scanButton = fileButton(page, "Scan bill");
  if (page.viewportSize()?.width === 320) await expectMinimumTarget(scanButton);
  await scanButton.focus();
  await expect(scanButton).toBeFocused();
  const billChooserPromise = page.waitForEvent("filechooser");
  await page.keyboard.press("Enter");
  const billChooser = await billChooserPromise;
  expect(billChooser.isMultiple()).toBe(false);
  await billChooser.setFiles({
    name: "bill.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-1.4 synthetic e2e"),
  });
  await expect(page.getByText("Processed — ready for review")).toBeVisible();

  await expect.poll(() => uploads.length).toBe(3);
  expect(uploads.filter((upload) => upload.intent === "generic_file").map(({ filename, intent }) => ({ filename, intent })).sort((left, right) => left.filename.localeCompare(right.filename))).toEqual([
    { filename: "mixed-ledger.csv", intent: "generic_file" },
    { filename: "mixed-ledger.xlsx", intent: "generic_file" },
  ]);
  expect(uploads.filter((upload) => upload.intent === "bill_scan").map(({ filename, intent }) => ({ filename, intent }))).toEqual([
    { filename: "bill.pdf", intent: "bill_scan" },
  ]);
  expect(new Set(uploads.map((upload) => upload.idempotencyKey)).size).toBe(3);
  expect(audit.apiRequests.filter((request) => request.startsWith("GET /intakes/intake-"))).toHaveLength(3);
  expect(audit.apiRequests.some((request) => request.includes("/status"))).toBe(false);
  await expectViewportContained(page);
  await expectNoUnexpectedTraffic(audit);
});

test("complete mapping stages decisions before a fresh vector preview and explicit activation", async ({ page }) => {
  const documentId = "document-sheet";
  const intakeId = "intake-sheet";
  const sourceFileId = "source-sheet";
  const batchId = "batch-sheet";
  const source = {
    source_intake_id: intakeId,
    source_file_id: sourceFileId,
    source_version: 1,
    source_sha256: SHA_SOURCE,
    normalized_sha256: SHA_NORMALIZED,
    structure_fingerprint: SHA_STRUCTURE,
  };
  let mappingPreviewRequest: Record<string, unknown> | null = null;
  let mappingSetRequest: Record<string, unknown> | null = null;
  let mappingApplyRequest: Record<string, unknown> | null = null;
  let decisionRequest: Record<string, unknown> | null = null;
  const activationRequests: Array<Record<string, unknown>> = [];
  let batchVersion = 1;
  let lifecycle: "open" | "ready_to_activate" | "active" = "open";
  let savedDecisions: Array<Record<string, unknown>> = [];
  let decisionCompleted = false;
  let activationPreviewCount = 0;

  const currentSummary = () => batchSummary({
    batchId,
    documentId,
    intakeId,
    sourceFileId,
    candidateCount: 2,
    batchVersion,
    pendingCount: savedDecisions.length ? 0 : 2,
    includedCount: savedDecisions.filter((decision) => decision.action === "include").length,
    excludedCount: savedDecisions.filter((decision) => decision.action === "exclude").length,
    lifecycle,
  });

  const audit = await installLocalApiMock(page, (request, url): MockResponse | undefined => {
    if (request.method() === "GET" && url.pathname === `/documents/${documentId}/schema-descriptors`) {
      return {
        json: {
          document_id: documentId,
          source,
          descriptors: [
            {
              table_locator: "transactions",
              ordered_headers: ["transaction_date", "total_amount"],
              inferred_types: ["date", "decimal"],
              row_count: 2,
              schema_fingerprint: "1".repeat(64),
            },
            {
              table_locator: "notes",
              ordered_headers: ["note"],
              inferred_types: ["string"],
              row_count: 1,
              schema_fingerprint: "2".repeat(64),
            },
          ],
        },
      };
    }
    if (request.method() === "GET" && url.pathname === `/documents/${documentId}/mappings`) {
      return { json: { document_id: documentId, source, items: [] } };
    }
    if (request.method() === "POST" && url.pathname === `/documents/${documentId}/mappings`) {
      const body = requestJson<Record<string, unknown>>(request);
      return {
        json: {
          ...body,
          id: "mapping-transactions",
          mapping_version: 1,
          mapping_digest: "3".repeat(64),
          created_at: CREATED_AT,
        },
      };
    }
    if (request.method() === "POST" && url.pathname === `/documents/${documentId}/mapping-sets/preview`) {
      mappingPreviewRequest = requestJson<Record<string, unknown>>(request);
      return {
        json: {
          document_id: documentId,
          source,
          previews: [
            {
              table_locator: "transactions",
              rows: [{
                row_ordinal: 1,
                source_locator: "transactions!row=2",
                values: { memo: '<img src="https://outside.invalid/steal.png" onerror="alert(1)">' },
                errors: [],
              }, {
                row_ordinal: 2,
                source_locator: "transactions!row=3",
                values: { memo: "second candidate" },
                errors: [],
              }],
              total_rows: 2,
              valid_rows: 2,
              error_rows: 0,
              blank_rows: 0,
              truncated: false,
            },
          ],
          reconciliation_counts: { pending: 2 },
          candidate_count: 2,
        },
      };
    }
    if (request.method() === "POST" && url.pathname === `/documents/${documentId}/mapping-sets`) {
      mappingSetRequest = requestJson<Record<string, unknown>>(request);
      const entries = mappingSetRequest.entries as Array<Record<string, unknown>>;
      return {
        json: {
          id: "mapping-set-1",
          document_id: documentId,
          source,
          set_digest: "4".repeat(64),
          version: 1,
          created_by: mappingSetRequest.created_by,
          created_at: CREATED_AT,
          entries: entries.map((entry, index) => ({ ...entry, ordinal: index })),
        },
      };
    }
    if (request.method() === "POST" && url.pathname === `/documents/${documentId}/mapping-sets/mapping-set-1/apply`) {
      mappingApplyRequest = requestJson<Record<string, unknown>>(request);
      return {
        json: {
          id: batchId,
          document_id: documentId,
          source_intake_id: intakeId,
          source_file_id: sourceFileId,
          source_version: 1,
          source_sha256: SHA_SOURCE,
          normalized_sha256: SHA_NORMALIZED,
          structure_fingerprint: SHA_STRUCTURE,
          mapping_set_id: "mapping-set-1",
          mapping_set_version: 1,
          mapping_set_digest: "4".repeat(64),
          lifecycle: "open",
          candidate_count: 2,
          reconciliation_counts: { pending: 2 },
          reconciliation_digest: "5".repeat(64),
          version: 1,
          replayed: false,
        },
      };
    }
    if (request.method() === "GET" && url.pathname === "/review/batches") {
      return { json: { items: [currentSummary()], total: 1, limit: 100, offset: 0 } };
    }
    if (request.method() === "GET" && url.pathname === `/review/batches/${batchId}/candidates`) {
      return {
        json: {
          batch_id: batchId,
          batch_version: batchVersion,
          items: [1, 2].map((ordinal) => candidate({
            batchId,
            ordinal,
            latestDecision: savedDecisions.find((decision) => decision.extraction_id === `extraction-${ordinal}`) ?? null,
          })),
          source_duplicate_evidence: [],
          total: 2,
          limit: 50,
          offset: 0,
        },
      };
    }
    if (request.method() === "GET" && url.pathname === `/intakes/${intakeId}`) {
      return { json: sourceIntake({ documentId, intakeId, sourceFileId, state: "needs_mapping" }) };
    }
    if (request.method() === "GET" && url.pathname === `/documents/${documentId}`) {
      return {
        json: documentRecord({
          documentId,
          sourceFileId,
          filename: "mixed-ledger.xlsx",
          mime: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }),
      };
    }
    if (request.method() === "POST" && url.pathname === `/review/batches/${batchId}/decisions`) {
      decisionRequest = requestJson<Record<string, unknown>>(request);
      const decisions = decisionRequest.decisions as Array<Record<string, unknown>>;
      savedDecisions = decisions.map((decision, index) => ({
        ...decision,
        id: `decision-${index + 1}`,
        decision_revision: 1,
        actor: decisionRequest?.actor,
        created_at: CREATED_AT,
      }));
      const previousBatchVersion = batchVersion;
      batchVersion += 1;
      lifecycle = "ready_to_activate";
      decisionCompleted = true;
      return {
        json: {
          batch_id: batchId,
          previous_batch_version: previousBatchVersion,
          batch_version: batchVersion,
          lifecycle,
          decisions: savedDecisions,
        },
      };
    }
    if (request.method() === "GET" && url.pathname === `/review/batches/${batchId}/activation-preview`) {
      if (!decisionCompleted) throw new Error("Activation preview was fetched before decision revisions were saved.");
      activationPreviewCount += 1;
      return {
        json: {
          batch_id: batchId,
          document_id: documentId,
          source_intake_id: intakeId,
          source_file_id: sourceFileId,
          source_version: 1,
          batch_version: batchVersion,
          lifecycle,
          total_count: 2,
          pending_count: 0,
          included_count: 1,
          excluded_count: 1,
          error_count: 0,
          reconciliation_counts: { included: 1, excluded: 1 },
          reconciliation_digest: batchVersion.toString(16).padStart(64, "0"),
          candidate_count_matches: true,
          source_is_current: true,
          requires_accept_exclusions: true,
          requires_accept_empty: false,
          ready_for_activation: true,
          activation_vector_sha256: activationPreviewCount === 1 ? SHA_VECTOR : SHA_REFRESHED_VECTOR,
          errors: [],
        },
      };
    }
    if (request.method() === "POST" && url.pathname === `/review/batches/${batchId}/activate`) {
      activationRequests.push(requestJson<Record<string, unknown>>(request));
      if (activationRequests.length === 1) {
        batchVersion += 1;
        return {
          status: 409,
          json: {
            code: "stale_activation_preview",
            message: "The activation vector changed on the local server.",
            detail: { batch_id: batchId, current_batch_version: batchVersion },
          },
        };
      }
      lifecycle = "active";
      batchVersion += 1;
      return {
        json: {
          batch_id: batchId,
          document_id: documentId,
          batch_version: batchVersion,
          lifecycle,
          activation_vector_sha256: SHA_REFRESHED_VECTOR,
          included_count: 1,
          excluded_count: 1,
          accepted_exclusions: true,
          accepted_empty: false,
          verified_by_extraction: { "extraction-1": "verified-1" },
        },
      };
    }
    return undefined;
  });

  await page.goto(`/#mapping/${documentId}`);
  await expect(page.getByRole("heading", { name: "Map source structure" })).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
  const cards = page.locator("section.mapping-card");
  await expect(cards).toHaveCount(2);
  const transactions = cards.filter({ has: page.getByRole("heading", { name: "transactions" }) });
  const notes = cards.filter({ has: page.getByRole("heading", { name: "notes" }) });
  const recordKind = transactions.getByLabel("Record kind");
  await tabTo(page, recordKind);
  await expect(recordKind).toBeFocused();
  await recordKind.press("f");
  await expect(recordKind).toHaveValue("financial");
  const financialSubtype = transactions.getByLabel("Financial subtype");
  await tabTo(page, financialSubtype);
  await financialSubtype.press("t");
  await expect(financialSubtype).toHaveValue("transaction");
  const mapNotes = notes.getByLabel("Map this structure");
  const ignoreNotes = notes.getByLabel("Ignore with a reason");
  await tabTo(page, mapNotes);
  await mapNotes.press("ArrowRight");
  await expect(ignoreNotes).toBeChecked();
  const ignoreReason = notes.getByLabel("Ignore reason");
  await tabTo(page, ignoreReason);
  await page.keyboard.type("Non-record annotations");

  const validateMapping = page.getByRole("button", { name: "Save definitions and preview" });
  await tabTo(page, validateMapping);
  await validateMapping.press("Enter");
  await expect(page.getByRole("heading", { name: "Escaped sample and reconciliation" })).toBeVisible();
  await expect(page.getByText(/outside\.invalid\/steal\.png/u)).toBeVisible();
  expect(mappingPreviewRequest).not.toBeNull();
  const completedMappingPreview = mappingPreviewRequest as unknown as Record<string, unknown>;
  const previewEntries = completedMappingPreview.entries as Array<Record<string, unknown>>;
  expect(completedMappingPreview.preview_limit).toBe(50);
  expect(previewEntries).toHaveLength(2);
  expect(previewEntries[0]).toMatchObject({ table_locator: "transactions", mapping_id: "mapping-transactions", mapping_version: 1 });
  expect(previewEntries[1]).toMatchObject({ table_locator: "notes", ignore_reason: "Non-record annotations" });

  const createBatch = page.getByRole("button", { name: "Create candidate batch" });
  await tabTo(page, createBatch);
  await createBatch.press("Enter");
  await expect(page.getByRole("heading", { name: "Grouped source review" })).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
  await expect(page.getByText("This source is attachment-only. It is never rendered as active browser content.")).toBeVisible();
  await expect(page.locator("section.evidence-pane .preview-frame img")).toHaveCount(0);
  await expect(page.getByText("These decisions are staged only. The previous active cohort remains authoritative until atomic activation succeeds.")).toBeVisible();
  await expect(page.getByText("This active batch is authoritative for its source. Included financial records may feed verified-only consumers.")).toHaveCount(0);
  expect(mappingSetRequest).not.toBeNull();
  expect(mappingApplyRequest).toMatchObject({
    mapping_set_version: 1,
    mapping_set_digest: "4".repeat(64),
    expected_mapping_versions: { "mapping-transactions": 1 },
  });

  const candidateRows = page.getByRole("table", { name: "Review candidates" }).locator("tbody > tr");
  await expect(candidateRows).toHaveCount(2);
  const includeFirst = candidateRows.nth(0).getByRole("radio", { name: "Include in activation" });
  await tabTo(page, includeFirst);
  await includeFirst.press("Space");
  const includeSecond = candidateRows.nth(1).getByRole("radio", { name: "Include in activation" });
  const excludeSecond = candidateRows.nth(1).getByRole("radio", { name: "Exclude from activation" });
  await tabTo(page, includeSecond);
  await includeSecond.press("ArrowRight");
  await expect(excludeSecond).toBeChecked();
  const exclusionReason = candidateRows.nth(1).getByLabel("Exclusion reason");
  await tabTo(page, exclusionReason);
  await page.keyboard.type("Duplicate statement summary");
  const saveDecisions = page.getByRole("button", { name: "Save this page's decisions" });
  await tabTo(page, saveDecisions);
  await saveDecisions.press("Enter");
  await expect(page.getByText("Saved 2 decision revisions. Candidates remain pending until activation.")).toBeVisible();
  await expect(page.getByText("These decisions are staged only. The previous active cohort remains authoritative until atomic activation succeeds.")).toBeVisible();
  expect(decisionRequest).toMatchObject({ expected_batch_version: 1, actor: "local-user" });
  const completedDecisionRequest = decisionRequest as unknown as Record<string, unknown>;
  expect(completedDecisionRequest.decisions as unknown[]).toHaveLength(2);

  const loadActivationPreview = page.getByRole("button", { name: "Load fresh activation preview" });
  await expect(loadActivationPreview).toBeEnabled();
  await tabTo(page, loadActivationPreview);
  await loadActivationPreview.press("Enter");
  await expect(page.getByText("I accept the explicitly excluded candidates in this complete batch.")).toBeVisible();
  if (page.viewportSize()?.width === 320) {
    await expectMinimumTarget(page.getByText("Complete reconciliation evidence", { exact: true }));
  }
  const activateButton = page.getByRole("button", { name: "Activate complete batch" });
  await expect(activateButton).toBeDisabled();
  const acceptExclusions = page.getByLabel("I accept the explicitly excluded candidates in this complete batch.");
  await tabTo(page, acceptExclusions);
  await acceptExclusions.press("Space");
  await expect(activateButton).toBeEnabled();
  await tabTo(page, activateButton);
  await activateButton.press("Enter");
  const previewButton = page.getByRole("button", { name: "Load fresh activation preview" });
  await expect(previewButton).toBeFocused();
  await expect(page.getByText("This active batch is authoritative for its source. Included financial records may feed verified-only consumers.")).toHaveCount(0);
  expect(activationPreviewCount).toBe(1);
  await page.keyboard.press("Enter");
  await expect(page.getByText("I accept the explicitly excluded candidates in this complete batch.")).toBeVisible();
  const refreshedConsent = page.getByLabel("I accept the explicitly excluded candidates in this complete batch.");
  await tabTo(page, refreshedConsent);
  await refreshedConsent.press("Space");
  const refreshedActivate = page.getByRole("button", { name: "Activate complete batch" });
  await tabTo(page, refreshedActivate);
  await refreshedActivate.press("Enter");
  await expect(page.getByText("This active batch is authoritative for its source. Included financial records may feed verified-only consumers.")).toBeVisible();
  expect(activationPreviewCount).toBe(2);
  expect(activationRequests).toEqual([
    {
      expected_batch_version: 2,
      expected_vector_sha256: SHA_VECTOR,
      actor: "local-user",
      accept_exclusions: true,
      accept_empty: false,
    },
    {
      expected_batch_version: 3,
      expected_vector_sha256: SHA_REFRESHED_VECTOR,
      actor: "local-user",
      accept_exclusions: true,
      accept_empty: false,
    },
  ]);
  await expectViewportContained(page);
  await expectNoUnexpectedTraffic(audit);
});

test("exact PDF source stays attachment-only while numbered inert PNG pages render", async ({ page }) => {
  const documentId = "document-pdf";
  const intakeId = "intake-pdf";
  const sourceFileId = "source-pdf";
  const batchId = "batch-pdf";
  const sourceVersion = 7;
  const previewPages: number[] = [];
  let rawOriginalRequests = 0;
  const audit = await installLocalApiMock(page, (request, url): MockResponse | undefined => {
    if (request.method() === "GET" && url.pathname === "/review/batches") {
      return {
        json: {
          items: [batchSummary({ batchId, documentId, intakeId, sourceFileId, sourceVersion, candidateCount: 1, pendingCount: 0, includedCount: 1, lifecycle: "active" })],
          total: 1,
          limit: 100,
          offset: 0,
        },
      };
    }
    if (request.method() === "GET" && url.pathname === `/review/batches/${batchId}/candidates`) {
      return {
        json: {
          batch_id: batchId,
          batch_version: 1,
          items: [candidate({ batchId, ordinal: 1 })],
          source_duplicate_evidence: [],
          total: 1,
          limit: 50,
          offset: 0,
        },
      };
    }
    if (request.method() === "GET" && url.pathname === `/intakes/${intakeId}`) {
      return { json: sourceIntake({ documentId, intakeId, sourceFileId, sourceVersion, intakeIntent: "bill_scan" }) };
    }
    if (request.method() === "GET" && url.pathname === `/documents/${documentId}`) {
      return { json: documentRecord({ documentId, sourceFileId, sourceVersion, filename: "receipt.pdf", mime: "application/pdf" }) };
    }
    if (request.method() === "GET" && url.pathname === `/documents/${documentId}/sources/${sourceFileId}/pdf-preview`) {
      return {
        json: {
          schema_version: 1,
          document_id: documentId,
          source_file_id: sourceFileId,
          source_version: sourceVersion,
          source_sha256: SHA_SOURCE,
          page_count: 2,
          status: "ready",
          pages: [
            { page_number: 1, artifact_id: "page-1", sha256: SHA_PAGE_ONE, mime: "image/png", width: 800, height: 1100, byte_size: PNG_1X1.length },
            { page_number: 2, artifact_id: "page-2", sha256: SHA_PAGE_TWO, mime: "image/png", width: 800, height: 1100, byte_size: PNG_1X1.length },
          ],
          manifest_sha256: "1".repeat(64),
          unavailable_reason: null,
        },
      };
    }
    const pageMatch = url.pathname.match(new RegExp(`^/documents/${documentId}/sources/${sourceFileId}/pdf-preview/pages/(\\d+)$`, "u"));
    if (request.method() === "GET" && pageMatch) {
      previewPages.push(Number(pageMatch[1]));
      return {
        body: PNG_1X1,
        contentType: "image/png",
        headers: {
          "Cache-Control": "no-store",
          "Content-Security-Policy": "default-src 'none'",
          "X-Content-Type-Options": "nosniff",
        },
      };
    }
    if (request.method() === "GET" && url.pathname === `/documents/${documentId}/original`) {
      rawOriginalRequests += 1;
      return {
        body: "%PDF-1.4 raw source must never be embedded",
        contentType: "application/pdf",
        headers: { "Content-Disposition": "attachment; filename=receipt.pdf", "X-Content-Type-Options": "nosniff" },
      };
    }
    return undefined;
  });

  await page.goto("/#review");
  await expect(page.getByRole("heading", { name: "Grouped source review" })).toBeVisible();
  const pageOne = page.getByRole("img", { name: "Inert PDF preview page 1 of 2" });
  await expect(pageOne).toBeVisible();
  await expect(pageOne).toHaveAttribute("src", new RegExp(`/pdf-preview/pages/1\\?version=${sourceVersion}&sha256=${SHA_SOURCE}$`, "u"));
  const download = page.getByRole("link", { name: "Download preserved original" });
  await expect(download).toHaveAttribute("href", new RegExp(`/original\\?version=${sourceVersion}&source_file_id=${sourceFileId}&sha256=${SHA_SOURCE}$`, "u"));
  await expect(download).toHaveAttribute("target", "_blank");
  await expect(page.locator("iframe, embed, object")).toHaveCount(0);
  expect(rawOriginalRequests).toBe(0);
  expect(previewPages).toEqual([1]);

  await page.getByLabel("PDF page controls").getByRole("button", { name: "Next" }).click();
  await expect(page.getByRole("img", { name: "Inert PDF preview page 2 of 2" })).toBeVisible();
  await expect.poll(() => previewPages).toEqual([1, 2]);
  expect(rawOriginalRequests).toBe(0);
  expect(audit.apiRequests.filter((entry) => entry.includes("/pdf-preview/pages/"))).toEqual([
    `GET /documents/${documentId}/sources/${sourceFileId}/pdf-preview/pages/1?version=${sourceVersion}&sha256=${SHA_SOURCE}`,
    `GET /documents/${documentId}/sources/${sourceFileId}/pdf-preview/pages/2?version=${sourceVersion}&sha256=${SHA_SOURCE}`,
  ]);
  await expectViewportContained(page);
  await expectNoUnexpectedTraffic(audit);
});

test("stale decision conflict preserves drafts and requires explicit resubmission", async ({ page }) => {
  const documentId = "document-stale";
  const intakeId = "intake-stale";
  const sourceFileId = "source-stale";
  const batchId = "batch-stale";
  let batchVersion = 1;
  let extractionVersion = 1;
  let decisionAttempts = 0;
  let persistedDecision: Record<string, unknown> | null = null;
  const submittedBodies: Array<Record<string, unknown>> = [];
  const audit = await installLocalApiMock(page, (request, url): MockResponse | undefined => {
    if (request.method() === "GET" && url.pathname === "/review/batches") {
      return {
        json: {
          items: [batchSummary({
            batchId,
            documentId,
            intakeId,
            sourceFileId,
            candidateCount: 1,
            batchVersion,
            pendingCount: persistedDecision ? 0 : 1,
            includedCount: persistedDecision ? 1 : 0,
            lifecycle: persistedDecision ? "ready_to_activate" : "open",
          })],
          total: 1,
          limit: 100,
          offset: 0,
        },
      };
    }
    if (request.method() === "GET" && url.pathname === `/review/batches/${batchId}/candidates`) {
      return {
        json: {
          batch_id: batchId,
          batch_version: batchVersion,
          items: [candidate({ batchId, ordinal: 1, version: extractionVersion, latestDecision: persistedDecision })],
          source_duplicate_evidence: [],
          total: 1,
          limit: 50,
          offset: 0,
        },
      };
    }
    if (request.method() === "GET" && url.pathname === `/intakes/${intakeId}`) {
      return { json: sourceIntake({ documentId, intakeId, sourceFileId }) };
    }
    if (request.method() === "GET" && url.pathname === `/documents/${documentId}`) {
      return { json: documentRecord({ documentId, sourceFileId }) };
    }
    if (request.method() === "POST" && url.pathname === `/review/batches/${batchId}/decisions`) {
      decisionAttempts += 1;
      const body = requestJson<Record<string, unknown>>(request);
      submittedBodies.push(body);
      if (decisionAttempts === 1) {
        batchVersion = 2;
        extractionVersion = 2;
        return {
          status: 409,
          json: {
            code: "stale_review_batch",
            message: "The batch changed on the local server.",
            detail: { affected_extraction_ids: ["extraction-1"] },
          },
        };
      }
      const decision = (body.decisions as Array<Record<string, unknown>>)[0];
      persistedDecision = {
        ...decision,
        id: "decision-stale-1",
        decision_revision: 1,
        actor: body.actor,
        created_at: CREATED_AT,
      };
      const previousBatchVersion = batchVersion;
      batchVersion = 3;
      return {
        json: {
          batch_id: batchId,
          previous_batch_version: previousBatchVersion,
          batch_version: batchVersion,
          lifecycle: "ready_to_activate",
          decisions: [persistedDecision],
        },
      };
    }
    return undefined;
  });

  await page.goto("/#review");
  const table = page.getByRole("table", { name: "Review candidates" });
  await expect(table.locator("tbody > tr")).toHaveCount(1);
  await table.getByRole("radio", { name: "Include in activation" }).check();
  const save = page.getByRole("button", { name: "Save this page's decisions" });
  await save.click();
  await expect(table.getByText("Changed on server")).toBeVisible();
  await expect(table.getByRole("radio", { name: "Include in activation" })).toBeChecked();
  await expect(save).toBeEnabled();
  expect(decisionAttempts).toBe(1);
  expect(submittedBodies[0]).toMatchObject({
    expected_batch_version: 1,
    decisions: [{ expected_extraction_version: 1, expected_decision_revision: 0, action: "include" }],
  });

  await save.click();
  await expect(page.getByText("Saved 1 decision revisions. Candidates remain pending until activation.")).toBeVisible();
  expect(decisionAttempts).toBe(2);
  expect(submittedBodies[1]).toMatchObject({
    expected_batch_version: 2,
    decisions: [{ expected_extraction_version: 2, expected_decision_revision: 0, action: "include" }],
  });
  await expectViewportContained(page);
  await expectNoUnexpectedTraffic(audit);
});

test("a generated 1,200-candidate batch fetches and renders only one 50-row server page", async ({ page }) => {
  const documentId = "document-scale";
  const intakeId = "intake-scale";
  const sourceFileId = "source-scale";
  const batchId = "batch-scale";
  const candidateQueries: string[] = [];
  let intakeRequests = 0;
  let documentRequests = 0;
  const audit = await installLocalApiMock(page, (request: Request, url): MockResponse | undefined => {
    if (request.method() === "GET" && url.pathname === "/review/batches") {
      return {
        json: {
          items: [batchSummary({ batchId, documentId, intakeId, sourceFileId, candidateCount: 1_200 })],
          total: 1,
          limit: 100,
          offset: 0,
        },
      };
    }
    if (request.method() === "GET" && url.pathname === `/review/batches/${batchId}/candidates`) {
      candidateQueries.push(url.search);
      const limit = Number(url.searchParams.get("limit"));
      const offset = Number(url.searchParams.get("offset"));
      if (limit !== 50 || ![0, 50].includes(offset)) throw new Error(`Expected a bounded candidate page, got ${url.search}.`);
      return {
        json: {
          batch_id: batchId,
          batch_version: 1,
          items: Array.from({ length: 50 }, (_, index) => candidate({ batchId, ordinal: offset + index + 1 })),
          source_duplicate_evidence: [],
          total: 1_200,
          limit,
          offset,
        },
      };
    }
    if (request.method() === "GET" && url.pathname === `/intakes/${intakeId}`) {
      intakeRequests += 1;
      return { json: sourceIntake({ documentId, intakeId, sourceFileId }) };
    }
    if (request.method() === "GET" && url.pathname === `/documents/${documentId}`) {
      documentRequests += 1;
      return { json: documentRecord({ documentId, sourceFileId, filename: "generated-1200.csv" }) };
    }
    return undefined;
  });

  await page.goto("/#review");
  const candidateTable = page.getByRole("table", { name: "Review candidates" });
  await expect(candidateTable.locator("tbody > tr")).toHaveCount(50);
  await expect(page.getByText("1–50 of 1200")).toBeVisible();
  expect(candidateQueries.length).toBeGreaterThan(0);
  expect(new Set(candidateQueries)).toEqual(new Set(["?limit=50&offset=0"]));
  expect(await candidateTable.locator("tbody > tr").count()).toBeLessThanOrEqual(50);
  await page.evaluate(() => {
    const measured = window as typeof window & { __clerksanLongTasks?: number[]; __clerksanLongTaskObserver?: PerformanceObserver };
    measured.__clerksanLongTasks = [];
    measured.__clerksanLongTaskObserver = new PerformanceObserver((entries) => {
      measured.__clerksanLongTasks?.push(...entries.getEntries().map((entry) => entry.duration));
    });
    measured.__clerksanLongTaskObserver.observe({ type: "longtask" });
  });
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByText("51–100 of 1200")).toBeVisible();
  await expect(candidateTable.getByText("row=52")).toBeVisible();
  const longTasks = await page.evaluate(() => (window as typeof window & { __clerksanLongTasks?: number[] }).__clerksanLongTasks ?? []);
  const maximumLongTaskMs = Math.max(0, ...longTasks);
  console.log(`PERFORMANCE_LONGTASK_MAX_MS=${maximumLongTaskMs.toFixed(2)}`);
  expect(maximumLongTaskMs).toBeLessThanOrEqual(100);
  expect(new Set(candidateQueries)).toEqual(new Set(["?limit=50&offset=0", "?limit=50&offset=50"]));
  expect(intakeRequests).toBe(1);
  expect(documentRequests).toBe(1);
  await expectViewportContained(page);
  await expectNoUnexpectedTraffic(audit);
});
