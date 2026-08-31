import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, LocalApiError } from "@/api/client";
import GroupedReviewWorkspace from "@/features/review/grouped-review-workspace";
import { I18nProvider, useI18n } from "@/lib/i18n";

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

function LocaleSwitches(): React.ReactElement {
  const { setLocale } = useI18n();
  return <><button onClick={() => setLocale("vi")}>VI</button><button onClick={() => setLocale("ja")}>JA</button></>;
}

function mockSingleBatchWorkspace() {
  const batch = { id: "batch-1", document_id: "document-1", source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, lifecycle: "open" as const, version: 1, candidate_count: 1, pending_count: 1, included_count: 0, excluded_count: 0, error_count: 0, exception_count: 0, reconciliation_counts: { mapped_candidate: 1 }, reconciliation_digest: "d".repeat(64), created_at: "2026-08-23T00:00:00Z", updated_at: "2026-08-23T00:00:00Z" };
  const page = { batch_id: "batch-1", batch_version: 1, total: 1, limit: 50, offset: 0, source_duplicate_evidence: [], items: [{ extraction_id: "extract-1", batch_id: "batch-1", candidate_ordinal: 1, candidate_key: "1".repeat(64), row_fingerprint: "2".repeat(64), record_kind: "generic_document" as const, financial_subtype: null, source_locator: "row/1", version: 1, status: "pending_review", payload: { title: "candidate" }, field_confidences: {}, source_spans: {}, validation_issues: [], evidence_group_keys: [], latest_decision: null, duplicate_evidence: [] }] };
  const reviewBatches = vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: [batch], total: 1, limit: 100, offset: 0 });
  const reviewCandidates = vi.spyOn(api, "reviewCandidates").mockResolvedValue(page);
  const intake = vi.spyOn(api, "intake").mockResolvedValue({ intake_id: "intake-1", document_id: "document-1", source_file_id: "source-1", source_version: 1, source_sha256: "a".repeat(64), intake_intent: "generic_file", state: "processed", retryable: false, version: 1 });
  const document = vi.spyOn(api, "document").mockResolvedValue({ id: "document-1", doc_class: "other", status: "in_review", source_filename: "records.csv", created_at: "2026-08-23T00:00:00Z", files: [{ id: "source-1", kind: "original", version: 1, source_filename: "records.csv", mime: "text/csv", sha256: "a".repeat(64) }] });
  return { batch, page, reviewBatches, reviewCandidates, intake, document };
}

describe("GroupedReviewWorkspace", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("stages record decisions without claiming authority and activates only from a fresh vector", async () => {
    const user = userEvent.setup();
    const batch = { id: "batch-1", document_id: "document-1", source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, lifecycle: "open" as const, version: 1, candidate_count: 2, pending_count: 2, included_count: 0, excluded_count: 0, error_count: 0, exception_count: 1, reconciliation_counts: { mapped_candidate: 2 }, reconciliation_digest: "d".repeat(64), created_at: "2026-08-23T00:00:00Z", updated_at: "2026-08-23T00:00:00Z" };
    const candidatePage = { batch_id: "batch-1", batch_version: 1, total: 2, limit: 50, offset: 0, source_duplicate_evidence: [{ id: "duplicate-1", suspected_document_id: "document-other", reason: "same source", score: 0.9, evidence: {}, scope: "source" }], items: [1, 2].map((ordinal) => ({ extraction_id: `extract-${ordinal}`, batch_id: "batch-1", candidate_ordinal: ordinal, candidate_key: String(ordinal).padStart(64, "0"), record_kind: "financial" as const, financial_subtype: "transaction" as const, source_locator: `row/${ordinal}`, version: 1, status: "pending_review", payload: { total_amount: ordinal * 100 }, field_confidences: {}, source_spans: {}, validation_issues: ordinal === 2 ? ["amount:check"] : [], evidence_group_keys: [], latest_decision: null, duplicate_evidence: [] })) };
    vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: [batch], total: 1, limit: 100, offset: 0 });
    vi.spyOn(api, "reviewCandidates").mockResolvedValue(candidatePage);
    vi.spyOn(api, "intake").mockResolvedValue({ intake_id: "intake-1", document_id: "document-1", source_file_id: "source-1", source_version: 1, source_sha256: "a".repeat(64), intake_intent: "generic_file", state: "processed", retryable: false, version: 1 });
    vi.spyOn(api, "document").mockResolvedValue({ id: "document-1", doc_class: "other", status: "in_review", source_filename: "records.csv", created_at: "2026-08-23T00:00:00Z", files: [{ id: "source-1", kind: "original", version: 1, source_filename: "records.csv", mime: "text/csv", sha256: "a".repeat(64) }] });
    const decide = vi.spyOn(api, "decideReviewBatch").mockResolvedValue({ batch_id: "batch-1", previous_batch_version: 1, batch_version: 2, lifecycle: "ready_to_activate", decisions: [] });
    vi.spyOn(api, "activationPreview").mockResolvedValue({ batch_id: "batch-1", document_id: "document-1", source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, batch_version: 2, lifecycle: "ready_to_activate", total_count: 2, pending_count: 0, included_count: 1, excluded_count: 1, error_count: 0, reconciliation_counts: {}, reconciliation_digest: "d".repeat(64), candidate_count_matches: true, source_is_current: true, requires_accept_exclusions: true, requires_accept_empty: false, ready_for_activation: true, activation_vector_sha256: "e".repeat(64), errors: [] });
    const activation = deferred<Awaited<ReturnType<typeof api.activateReviewBatch>>>();
    const activate = vi.spyOn(api, "activateReviewBatch").mockReturnValue(activation.promise);

    render(<I18nProvider><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);
    expect(await screen.findByText(/previous active cohort remains authoritative/i)).toBeInTheDocument();
    fireEvent.click((await screen.findAllByRole("radio", { name: "Exclude from activation" }))[1]);
    fireEvent.change(screen.getByLabelText("Exclusion reason"), { target: { value: "not accounting" } });
    fireEvent.click(screen.getByRole("button", { name: "Save this page's decisions" }));
    await waitFor(() => expect(decide).toHaveBeenCalledTimes(1));
    expect(decide.mock.calls[0]?.[1].decisions[0]).toMatchObject({ action: "exclude", expected_extraction_version: 1, expected_decision_revision: 0, exclusion_reason: "not accounting" });

    fireEvent.click(screen.getByRole("button", { name: "Load fresh activation preview" }));
    await screen.findByText("I accept the explicitly excluded candidates in this complete batch.");
    expect(screen.getByText(/Source-level duplicate evidence/).closest("details")).toHaveClass("batch-disclosure");
    expect(screen.getByText("Complete reconciliation evidence").closest("details")).toHaveClass("batch-disclosure");
    const consent = screen.getByLabelText("I accept the explicitly excluded candidates in this complete batch.");
    const actor = screen.getByLabelText("Reviewer");
    const reason = screen.getByLabelText("Rejection reason");
    const reprocessConfirmation = screen.getByLabelText("I confirm this complete source batch should be rejected and reprocessed.");
    fireEvent.click(consent);
    fireEvent.change(reason, { target: { value: "keep this reason" } });
    fireEvent.click(reprocessConfirmation);
    fireEvent.click(screen.getByRole("button", { name: "Activate complete batch" }));
    await waitFor(() => expect(activate).toHaveBeenCalledWith("batch-1", expect.objectContaining({ expected_batch_version: 2, expected_vector_sha256: "e".repeat(64), accept_exclusions: true })));
    expect(consent).toBeDisabled();
    expect(consent).toBeChecked();
    expect(actor).toBeDisabled();
    expect(reason).toBeDisabled();
    expect(reason).toHaveValue("keep this reason");
    expect(reprocessConfirmation).toBeDisabled();
    expect(reprocessConfirmation).toBeChecked();
    await user.click(consent);
    await user.type(actor, "intruder");
    await user.type(reason, " changed");
    await user.click(reprocessConfirmation);
    expect(consent).toBeChecked();
    expect(actor).toHaveValue("local-user");
    expect(reason).toHaveValue("keep this reason");
    expect(reprocessConfirmation).toBeChecked();
    act(() => { activation.resolve({ batch_id: "batch-1", document_id: "document-1", batch_version: 3, lifecycle: "active", activation_vector_sha256: "e".repeat(64), included_count: 1, excluded_count: 1, accepted_exclusions: true, accepted_empty: false, verified_by_extraction: { "extract-1": "verified-1" } }); });
    await waitFor(() => expect(screen.getByText(/Activated the complete batch/)).toBeInTheDocument());
  });

  it("keeps candidates, exact source, and destructive confirmation bound to the selected batch", async () => {
    const batch = (id: string, ordinal: number) => ({ id, document_id: `document-${ordinal}`, source_intake_id: `intake-${ordinal}`, source_file_id: `source-${ordinal}`, source_version: 1, lifecycle: "open" as const, version: 1, candidate_count: 1, pending_count: 1, included_count: 0, excluded_count: 0, error_count: 0, exception_count: 0, reconciliation_counts: { mapped_candidate: 1 }, reconciliation_digest: String(ordinal).repeat(64), created_at: "2026-08-23T00:00:00Z", updated_at: "2026-08-23T00:00:00Z" });
    const page = (id: string, ordinal: number) => ({ batch_id: id, batch_version: 1, total: 1, limit: 50, offset: 0, source_duplicate_evidence: [], items: [{ extraction_id: `extract-${ordinal}`, batch_id: id, candidate_ordinal: ordinal, candidate_key: String(ordinal).padStart(64, "0"), row_fingerprint: String(ordinal).repeat(64), record_kind: "generic_document" as const, financial_subtype: null, source_locator: `row/${ordinal}`, version: 1, status: "pending_review", payload: { title: `batch ${ordinal}` }, field_confidences: {}, source_spans: {}, validation_issues: [], evidence_group_keys: [], latest_decision: null, duplicate_evidence: [] }] });
    let resolveFirst!: (value: ReturnType<typeof page>) => void;
    let resolveSecond!: (value: ReturnType<typeof page>) => void;
    const first = new Promise<ReturnType<typeof page>>((resolve) => { resolveFirst = resolve; });
    const second = new Promise<ReturnType<typeof page>>((resolve) => { resolveSecond = resolve; });
    vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: [batch("batch-1", 1), batch("batch-2", 2)], total: 2, limit: 100, offset: 0 });
    vi.spyOn(api, "reviewCandidates").mockImplementation((id) => id === "batch-1" ? first : second);
    const intake = vi.spyOn(api, "intake").mockImplementation(async (id) => ({ intake_id: id, document_id: id.replace("intake", "document"), source_file_id: id.replace("intake", "source"), source_version: 1, source_sha256: id.endsWith("1") ? "1".repeat(64) : "2".repeat(64), intake_intent: "generic_file", state: "processed", retryable: false, version: 1 }));
    const document = vi.spyOn(api, "document").mockImplementation(async (id) => ({ id, doc_class: "other", status: "in_review", source_filename: `${id}.csv`, created_at: "2026-08-23T00:00:00Z", files: [{ id: id.replace("document", "source"), kind: "original", version: 1, source_filename: `${id}.csv`, mime: "text/csv", sha256: id.endsWith("1") ? "1".repeat(64) : "2".repeat(64) }] }));

    render(<I18nProvider><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);
    const selector = await screen.findByLabelText("Source-bound batch");
    fireEvent.change(screen.getByLabelText("Rejection reason"), { target: { value: "retry this source" } });
    fireEvent.click(screen.getByLabelText("I confirm this complete source batch should be rejected and reprocessed."));
    fireEvent.change(selector, { target: { value: "batch-2" } });
    expect(screen.getByLabelText("Rejection reason")).toHaveValue("");
    expect(screen.getByLabelText("I confirm this complete source batch should be rejected and reprocessed.")).not.toBeChecked();
    resolveSecond(page("batch-2", 2));
    expect(await screen.findByText("row/2")).toBeInTheDocument();
    expect(screen.getByText("document-2.csv")).toBeInTheDocument();
    resolveFirst(page("batch-1", 1));
    await waitFor(() => expect(screen.queryByText("row/1")).not.toBeInTheDocument());
    expect(screen.getByText("row/2")).toBeInTheDocument();
    expect(intake).toHaveBeenCalledTimes(2);
    expect(document).toHaveBeenCalledTimes(2);
  });

  it("keeps unsaved decision drafts across locale changes and blocks queue reload", async () => {
    const { reviewBatches, reviewCandidates } = mockSingleBatchWorkspace();
    render(<I18nProvider><LocaleSwitches /><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);

    const exclude = await screen.findByRole("radio", { name: "Exclude from activation" });
    fireEvent.click(exclude);
    const reason = screen.getByLabelText("Exclusion reason");
    fireEvent.change(reason, { target: { value: "keep this draft" } });
    const reload = screen.getByRole("button", { name: "Reload queue" });
    expect(reload).toBeDisabled();
    expect(screen.getByLabelText("Rejection reason")).toBeDisabled();
    expect(screen.getByLabelText("I confirm this complete source batch should be rejected and reprocessed.")).toBeDisabled();
    fireEvent.click(reload);
    fireEvent.click(screen.getByRole("button", { name: "VI" }));
    expect(exclude).toBeChecked();
    expect(reason).toHaveValue("keep this draft");
    fireEvent.click(screen.getByRole("button", { name: "JA" }));
    expect(exclude).toBeChecked();
    expect(reason).toHaveValue("keep this draft");
    expect(reviewBatches).toHaveBeenCalledTimes(1);
    expect(reviewCandidates).toHaveBeenCalledTimes(1);
  });

  it("freezes the captured reject-and-reprocess form while its request is pending", async () => {
    const user = userEvent.setup();
    mockSingleBatchWorkspace();
    const reprocess = deferred<Awaited<ReturnType<typeof api.rejectAndReprocessBatch>>>();
    const reject = vi.spyOn(api, "rejectAndReprocessBatch").mockReturnValue(reprocess.promise);
    render(<I18nProvider><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);

    await screen.findByText("row/1");
    const actor = screen.getByLabelText("Reviewer");
    const reason = screen.getByLabelText("Rejection reason");
    const confirmation = screen.getByLabelText("I confirm this complete source batch should be rejected and reprocessed.");
    fireEvent.change(actor, { target: { value: "reviewer-a" } });
    fireEvent.change(reason, { target: { value: "bad mapping" } });
    fireEvent.click(confirmation);
    fireEvent.click(screen.getByRole("button", { name: "Reject and reprocess" }));

    await waitFor(() => expect(reject).toHaveBeenCalledWith("batch-1", 1, "bad mapping", "reviewer-a"));
    expect(actor).toBeDisabled();
    expect(reason).toBeDisabled();
    expect(confirmation).toBeDisabled();
    await user.type(actor, "intruder");
    await user.type(reason, " changed");
    await user.click(confirmation);
    expect(actor).toHaveValue("reviewer-a");
    expect(reason).toHaveValue("bad mapping");
    expect(confirmation).toBeChecked();
    act(() => { reprocess.resolve({ batch_id: "batch-1", document_id: "document-1", source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, batch_version: 2, lifecycle: "rejected", status: "queued", job_id: "job-1" }); });
    await waitFor(() => expect(screen.getByText(/queued for reprocessing/i)).toBeInTheDocument());
  });

  it("fails closed when intake metadata does not match the selected batch identity", async () => {
    const { intake, document } = mockSingleBatchWorkspace();
    intake.mockResolvedValue({ intake_id: "intake-1", document_id: "document-1", source_file_id: "source-other", source_version: 2, source_sha256: "b".repeat(64), intake_intent: "generic_file", state: "processed", retryable: false, version: 1 });

    render(<I18nProvider><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);

    expect(await screen.findByText("The exact source identity is unavailable.")).toBeInTheDocument();
    expect(document).not.toHaveBeenCalled();
    expect(screen.queryByText("records.csv")).not.toBeInTheDocument();
  });

  it("fails closed when matching intake and file checksums are not lowercase SHA-256", async () => {
    const { intake, document } = mockSingleBatchWorkspace();
    intake.mockResolvedValue({ intake_id: "intake-1", document_id: "document-1", source_file_id: "source-1", source_version: 1, source_sha256: "A".repeat(64), intake_intent: "generic_file", state: "processed", retryable: false, version: 1 });
    document.mockResolvedValue({ id: "document-1", doc_class: "other", status: "in_review", source_filename: "records.csv", created_at: "2026-08-23T00:00:00Z", files: [{ id: "source-1", kind: "original", version: 1, source_filename: "records.csv", mime: "text/csv", sha256: "A".repeat(64) }] });

    render(<I18nProvider><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);

    expect(await screen.findByText("The exact source identity is unavailable.")).toBeInTheDocument();
    expect(document).not.toHaveBeenCalled();
    expect(screen.queryByText("records.csv")).not.toBeInTheDocument();
  });

  it("distinguishes source-bound batch options and localizes their lifecycle", async () => {
    const makeBatch = (id: string, intakeId: string, version: number) => ({ id, document_id: "document-shared", source_intake_id: intakeId, source_file_id: `source-${version}`, source_version: version, lifecycle: "open" as const, version: 1, candidate_count: 1, pending_count: 1, included_count: 0, excluded_count: 0, error_count: 0, exception_count: 0, reconciliation_counts: { mapped_candidate: 1 }, reconciliation_digest: String(version).repeat(64), created_at: "2026-08-23T00:00:00Z", updated_at: "2026-08-23T00:00:00Z" });
    const batches = [makeBatch("batch-alpha-0001", "intake-alpha-0001", 1), makeBatch("batch-beta-0002", "intake-beta-0002", 2)];
    vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: batches, total: 2, limit: 100, offset: 0 });
    vi.spyOn(api, "reviewCandidates").mockResolvedValue({ batch_id: batches[0].id, batch_version: 1, total: 0, limit: 50, offset: 0, source_duplicate_evidence: [], items: [] });
    vi.spyOn(api, "intake").mockResolvedValue({ intake_id: batches[0].source_intake_id, document_id: batches[0].document_id, source_file_id: batches[0].source_file_id, source_version: 1, source_sha256: "a".repeat(64), intake_intent: "generic_file", state: "processed", retryable: false, version: 1 });
    vi.spyOn(api, "document").mockResolvedValue({ id: "document-shared", doc_class: "other", status: "in_review", source_filename: "records.csv", created_at: "2026-08-23T00:00:00Z", files: [{ id: "source-1", kind: "original", version: 1, source_filename: "records.csv", mime: "text/csv", sha256: "a".repeat(64) }] });

    render(<I18nProvider><LocaleSwitches /><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);

    expect(await screen.findByRole("option", { name: /document · v1 · batch-al · intake-a · Open · 1/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /document · v2 · batch-be · intake-b · Open · 1/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "VI" }));
    expect(screen.getByRole("option", { name: /document · v1 · batch-al · intake-a · Đang mở · 1/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Open/ })).not.toBeInTheDocument();
  });

  it("clears old batch-bound state without auto-selecting a different batch", async () => {
    const makeBatch = (id: string, ordinal: number) => ({ id, document_id: `document-${ordinal}`, source_intake_id: `intake-${ordinal}`, source_file_id: `source-${ordinal}`, source_version: ordinal, lifecycle: "open" as const, version: 1, candidate_count: 1, pending_count: 1, included_count: 0, excluded_count: 0, error_count: 0, exception_count: 0, reconciliation_counts: { mapped_candidate: 1 }, reconciliation_digest: String(ordinal).repeat(64), created_at: "2026-08-23T00:00:00Z", updated_at: "2026-08-23T00:00:00Z" });
    const firstBatch = makeBatch("batch-1", 1);
    const secondBatch = makeBatch("batch-2", 2);
    const replacement = deferred<Awaited<ReturnType<typeof api.reviewBatches>>>();
    vi.spyOn(api, "reviewBatches")
      .mockResolvedValueOnce({ items: [firstBatch], total: 1, limit: 100, offset: 0 })
      .mockReturnValueOnce(replacement.promise);
    vi.spyOn(api, "reviewCandidates")
      .mockResolvedValueOnce({ batch_id: "batch-1", batch_version: 1, total: 1, limit: 50, offset: 0, source_duplicate_evidence: [], items: [{ extraction_id: "extract-1", batch_id: "batch-1", candidate_ordinal: 1, candidate_key: "1".repeat(64), row_fingerprint: "2".repeat(64), record_kind: "generic_document", financial_subtype: null, source_locator: "row/old", version: 1, status: "pending_review", payload: { title: "old candidate" }, field_confidences: {}, source_spans: {}, validation_issues: [], evidence_group_keys: [], latest_decision: null, duplicate_evidence: [] }] });
    vi.spyOn(api, "intake").mockImplementation(async (id) => ({ intake_id: id, document_id: id.replace("intake", "document"), source_file_id: id.replace("intake", "source"), source_version: Number(id.at(-1)), source_sha256: id.endsWith("1") ? "a".repeat(64) : "b".repeat(64), intake_intent: "generic_file", state: "processed", retryable: false, version: 1 }));
    vi.spyOn(api, "document").mockImplementation(async (id) => ({ id, doc_class: "other", status: "in_review", source_filename: `${id}.csv`, created_at: "2026-08-23T00:00:00Z", files: [{ id: id.replace("document", "source"), kind: "original", version: Number(id.at(-1)), source_filename: `${id}.csv`, mime: "text/csv", sha256: id.endsWith("1") ? "a".repeat(64) : "b".repeat(64) }] }));
    vi.spyOn(api, "activationPreview").mockResolvedValue({ batch_id: "batch-1", document_id: "document-1", source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, batch_version: 1, lifecycle: "open", total_count: 1, pending_count: 1, included_count: 0, excluded_count: 0, error_count: 0, reconciliation_counts: {}, reconciliation_digest: "d".repeat(64), candidate_count_matches: true, source_is_current: true, requires_accept_exclusions: false, requires_accept_empty: false, ready_for_activation: false, activation_vector_sha256: "e".repeat(64), errors: [] });

    render(<I18nProvider><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);
    expect(await screen.findByText("row/old")).toBeInTheDocument();
    expect(screen.getByText("document-1.csv")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Rejection reason"), { target: { value: "old reason" } });
    fireEvent.click(screen.getByLabelText("I confirm this complete source batch should be rejected and reprocessed."));
    fireEvent.click(screen.getByRole("button", { name: "Load fresh activation preview" }));
    expect(await screen.findByText("Complete reconciliation evidence")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Reload queue" }));
    act(() => { replacement.resolve({ items: [secondBatch], total: 1, limit: 100, offset: 0 }); });
    expect(await screen.findByText("Changed on server. Reload queue.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Source-bound batch")).not.toBeInTheDocument();
    expect(screen.queryByText("row/old")).not.toBeInTheDocument();
    expect(screen.queryByText("document-1.csv")).not.toBeInTheDocument();
    expect(screen.queryByText("Complete reconciliation evidence")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Rejection reason")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("I confirm this complete source batch should be rejected and reprocessed.")).not.toBeInTheDocument();
  });

  it("preserves a batch refresh failure while the candidate page refreshes", async () => {
    const { batch, reviewBatches, reviewCandidates } = mockSingleBatchWorkspace();
    reviewBatches
      .mockResolvedValueOnce({ items: [batch], total: 1, limit: 100, offset: 0 })
      .mockRejectedValueOnce(new Error("batch refresh failed"));
    vi.spyOn(api, "decideReviewBatch").mockResolvedValue({ batch_id: "batch-1", previous_batch_version: 1, batch_version: 2, lifecycle: "ready_to_activate", decisions: [] });
    render(<I18nProvider><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);

    fireEvent.click(await screen.findByRole("radio", { name: "Include in activation" }));
    fireEvent.click(screen.getByRole("button", { name: "Save this page's decisions" }));

    expect(await screen.findByText("batch refresh failed")).toBeInTheDocument();
    await waitFor(() => expect(reviewCandidates).toHaveBeenCalledTimes(2));
    expect(screen.getByText("batch refresh failed")).toBeInTheDocument();
  });

  it("keeps the stale activation message visible after refreshing the candidate page", async () => {
    const { page, reviewCandidates } = mockSingleBatchWorkspace();
    const refreshedPage = deferred<Awaited<ReturnType<typeof api.reviewCandidates>>>();
    reviewCandidates.mockResolvedValueOnce(page).mockReturnValueOnce(refreshedPage.promise);
    vi.spyOn(api, "activationPreview").mockResolvedValue({ batch_id: "batch-1", document_id: "document-1", source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, batch_version: 1, lifecycle: "ready_to_activate", total_count: 1, pending_count: 0, included_count: 1, excluded_count: 0, error_count: 0, reconciliation_counts: {}, reconciliation_digest: "d".repeat(64), candidate_count_matches: true, source_is_current: true, requires_accept_exclusions: false, requires_accept_empty: false, ready_for_activation: true, activation_vector_sha256: "e".repeat(64), errors: [] });
    vi.spyOn(api, "activateReviewBatch").mockRejectedValue(new LocalApiError(409, { code: "stale_activation_preview", message: "changed" }));
    render(<I18nProvider><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);

    const preview = await screen.findByRole("button", { name: "Load fresh activation preview" });
    fireEvent.click(preview);
    fireEvent.click(await screen.findByRole("button", { name: "Activate complete batch" }));

    await waitFor(() => expect(reviewCandidates).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("The activation vector changed. Load a fresh preview before confirming again.")).not.toBeInTheDocument();
    act(() => { refreshedPage.resolve(page); });
    expect(await screen.findByText("The activation vector changed. Load a fresh preview before confirming again.")).toBeInTheDocument();
    await waitFor(() => expect(preview).toHaveFocus());
  });

  it("explains a missing reviewer instead of silently ignoring activation", async () => {
    mockSingleBatchWorkspace();
    vi.spyOn(api, "activationPreview").mockResolvedValue({ batch_id: "batch-1", document_id: "document-1", source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, batch_version: 1, lifecycle: "ready_to_activate", total_count: 1, pending_count: 0, included_count: 1, excluded_count: 0, error_count: 0, reconciliation_counts: {}, reconciliation_digest: "d".repeat(64), candidate_count_matches: true, source_is_current: true, requires_accept_exclusions: false, requires_accept_empty: false, ready_for_activation: true, activation_vector_sha256: "e".repeat(64), errors: [] });
    const activate = vi.spyOn(api, "activateReviewBatch");
    render(<I18nProvider><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);

    fireEvent.click(await screen.findByRole("button", { name: "Load fresh activation preview" }));
    const reviewer = screen.getByLabelText("Reviewer");
    fireEvent.change(reviewer, { target: { value: " " } });
    expect(reviewer).toHaveAttribute("aria-invalid", "true");
    fireEvent.click(await screen.findByRole("button", { name: "Activate complete batch" }));

    expect(await screen.findByText("A reviewer name is required before approval.")).toBeInTheDocument();
    expect(activate).not.toHaveBeenCalled();
  });

  it("restores the stale decision notice and affected draft after the forced reload", async () => {
    const { page, reviewCandidates } = mockSingleBatchWorkspace();
    const refreshedPage = deferred<Awaited<ReturnType<typeof api.reviewCandidates>>>();
    reviewCandidates.mockResolvedValueOnce(page).mockReturnValueOnce(refreshedPage.promise);
    vi.spyOn(api, "decideReviewBatch").mockRejectedValue(new LocalApiError(409, { code: "stale_review_batch", message: "changed", detail: { affected_extraction_ids: ["extract-1"] } }));
    render(<I18nProvider><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);

    fireEvent.click(await screen.findByRole("radio", { name: "Include in activation" }));
    fireEvent.click(screen.getByRole("button", { name: "Save this page's decisions" }));
    await waitFor(() => expect(reviewCandidates).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/Drafts remain in memory/)).not.toBeInTheDocument();
    act(() => { refreshedPage.resolve(page); });

    expect(await screen.findByText(/Drafts remain in memory/)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Include in activation" })).toBeChecked();
    expect(screen.getByText("Changed on server")).toBeInTheDocument();
  });

  it("restores the stale reprocess notice and captured confirmation after the forced reload", async () => {
    const { page, reviewCandidates } = mockSingleBatchWorkspace();
    const refreshedPage = deferred<Awaited<ReturnType<typeof api.reviewCandidates>>>();
    reviewCandidates.mockResolvedValueOnce(page).mockReturnValueOnce(refreshedPage.promise);
    vi.spyOn(api, "rejectAndReprocessBatch").mockRejectedValue(new LocalApiError(409, { code: "stale_review_batch", message: "changed" }));
    render(<I18nProvider><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);

    await screen.findByText("row/1");
    fireEvent.change(screen.getByLabelText("Rejection reason"), { target: { value: "retry exact source" } });
    fireEvent.click(screen.getByLabelText("I confirm this complete source batch should be rejected and reprocessed."));
    fireEvent.click(screen.getByRole("button", { name: "Reject and reprocess" }));
    await waitFor(() => expect(reviewCandidates).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/Drafts remain in memory/)).not.toBeInTheDocument();
    act(() => { refreshedPage.resolve(page); });

    expect(await screen.findByText(/Drafts remain in memory/)).toBeInTheDocument();
    expect(screen.getByLabelText("Rejection reason")).toHaveValue("retry exact source");
    expect(screen.getByLabelText("I confirm this complete source batch should be rejected and reprocessed.")).toBeChecked();
  });

  it("evicts an unreconciled exact-source cache entry so reload can recover", async () => {
    const { batch, reviewBatches, document } = mockSingleBatchWorkspace();
    reviewBatches.mockImplementation(async () => ({ items: [batch], total: 1, limit: 100, offset: 0 }));
    document
      .mockResolvedValueOnce({ id: "document-1", doc_class: "other", status: "in_review", source_filename: "records.csv", created_at: "2026-08-23T00:00:00Z", files: [{ id: "source-1", kind: "original", version: 1, source_filename: "records.csv", mime: "text/csv", sha256: "b".repeat(64) }] })
      .mockResolvedValueOnce({ id: "document-1", doc_class: "other", status: "in_review", source_filename: "records.csv", created_at: "2026-08-23T00:00:00Z", files: [{ id: "source-1", kind: "original", version: 1, source_filename: "records.csv", mime: "text/csv", sha256: "a".repeat(64) }] });
    render(<I18nProvider><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);

    expect(await screen.findByText("The exact source identity is unavailable.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reload queue" }));
    expect(await screen.findByText("records.csv")).toBeInTheDocument();
    expect(document).toHaveBeenCalledTimes(2);
  });

  it("keeps the selected batch when a saved decision moves it to another server page", async () => {
    const makeBatch = (id: string, ordinal: number, lifecycle: "open" | "ready_to_activate" = "open") => ({ id, document_id: `document-${ordinal}`, source_intake_id: `intake-${ordinal}`, source_file_id: `source-${ordinal}`, source_version: 1, lifecycle, version: lifecycle === "open" ? 1 : 2, candidate_count: 1, pending_count: lifecycle === "open" ? 1 : 0, included_count: lifecycle === "open" ? 0 : 1, excluded_count: 0, error_count: 0, exception_count: 0, reconciliation_counts: { mapped_candidate: 1 }, reconciliation_digest: String(ordinal % 10).repeat(64), created_at: "2026-08-23T00:00:00Z", updated_at: "2026-08-23T00:00:00Z" });
    const firstPage = Array.from({ length: 50 }, (_, index) => makeBatch(`batch-${index + 1}`, index + 1));
    const movingOpen = makeBatch("batch-moving", 51);
    const movingReady = makeBatch("batch-moving", 51, "ready_to_activate");
    let decisionSaved = false;
    const reviewBatches = vi.spyOn(api, "reviewBatches").mockImplementation(async ({ offset = 0 } = {}) => {
      if (!decisionSaved) return { items: offset === 0 ? firstPage : [movingOpen], total: 51, limit: 50, offset };
      return { items: offset === 0 ? [movingReady, ...firstPage.slice(0, 49)] : [firstPage[49]], total: 51, limit: 50, offset };
    });
    vi.spyOn(api, "reviewCandidates").mockImplementation(async (batchId) => {
      const ordinal = batchId === "batch-moving" ? 51 : Number(batchId.split("-").at(-1));
      return { batch_id: batchId, batch_version: batchId === "batch-moving" && decisionSaved ? 2 : 1, total: 1, limit: 50, offset: 0, source_duplicate_evidence: [], items: [{ extraction_id: `extract-${ordinal}`, batch_id: batchId, candidate_ordinal: ordinal, candidate_key: String(ordinal).padStart(64, "0"), row_fingerprint: String(ordinal % 10).repeat(64), record_kind: "generic_document", financial_subtype: null, source_locator: `row/${ordinal}`, version: 1, status: "pending_review", payload: {}, field_confidences: {}, source_spans: {}, validation_issues: [], evidence_group_keys: [], latest_decision: null, duplicate_evidence: [] }] };
    });
    vi.spyOn(api, "intake").mockImplementation(async (intakeId) => {
      const ordinal = Number(intakeId.split("-").at(-1));
      return { intake_id: intakeId, document_id: `document-${ordinal}`, source_file_id: `source-${ordinal}`, source_version: 1, source_sha256: String(ordinal % 10).repeat(64), intake_intent: "generic_file", state: "processed", retryable: false, version: 1 };
    });
    vi.spyOn(api, "document").mockImplementation(async (documentId) => {
      const ordinal = Number(documentId.split("-").at(-1));
      return { id: documentId, doc_class: "other", status: "in_review", source_filename: `${documentId}.csv`, created_at: "2026-08-23T00:00:00Z", files: [{ id: `source-${ordinal}`, kind: "original", version: 1, source_filename: `${documentId}.csv`, mime: "text/csv", sha256: String(ordinal % 10).repeat(64) }] };
    });
    vi.spyOn(api, "decideReviewBatch").mockImplementation(async () => {
      decisionSaved = true;
      return { batch_id: "batch-moving", previous_batch_version: 1, batch_version: 2, lifecycle: "ready_to_activate", decisions: [] };
    });
    const activationPreview = vi.spyOn(api, "activationPreview").mockResolvedValue({ batch_id: "batch-moving", document_id: "document-51", source_intake_id: "intake-51", source_file_id: "source-51", source_version: 1, batch_version: 2, lifecycle: "ready_to_activate", total_count: 1, pending_count: 0, included_count: 1, excluded_count: 0, error_count: 0, reconciliation_counts: {}, reconciliation_digest: "d".repeat(64), candidate_count_matches: true, source_is_current: true, requires_accept_exclusions: false, requires_accept_empty: false, ready_for_activation: true, activation_vector_sha256: "e".repeat(64), errors: [] });

    render(<I18nProvider><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);
    await screen.findByText("row/1");
    const enabledNext = () => screen.getAllByRole<HTMLButtonElement>("button", { name: "Next" }).find((button) => !button.disabled) as HTMLButtonElement;
    fireEvent.click(enabledNext());
    expect(await screen.findByText("row/51")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Include in activation" }));
    fireEvent.click(screen.getByRole("button", { name: "Save this page's decisions" }));

    await waitFor(() => expect(reviewBatches).toHaveBeenCalledWith({ limit: 50, offset: 0 }));
    await waitFor(() => expect(screen.getByLabelText("Source-bound batch")).toHaveValue("batch-moving"));
    expect(await screen.findByText("row/51")).toBeInTheDocument();
    expect(screen.queryByText("row/50")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load fresh activation preview" }));
    await waitFor(() => expect(activationPreview).toHaveBeenCalledWith("batch-moving"));
  });

  it("pages through the server batch cohort to reach an actionable batch beyond the first 100", async () => {
    const makeBatch = (ordinal: number) => ({ id: `batch-${ordinal}`, document_id: `document-${ordinal}`, source_intake_id: `intake-${ordinal}`, source_file_id: `source-${ordinal}`, source_version: 1, lifecycle: "open" as const, version: 1, candidate_count: 1, pending_count: 1, included_count: 0, excluded_count: 0, error_count: 0, exception_count: 0, reconciliation_counts: { mapped_candidate: 1 }, reconciliation_digest: String(ordinal % 10).repeat(64), created_at: "2026-08-23T00:00:00Z", updated_at: "2026-08-23T00:00:00Z" });
    const firstPage = Array.from({ length: 50 }, (_, index) => makeBatch(index + 1));
    const secondPage = Array.from({ length: 50 }, (_, index) => makeBatch(index + 51));
    const finalPage = [makeBatch(101)];
    const reviewBatches = vi.spyOn(api, "reviewBatches").mockImplementation(async ({ offset = 0 } = {}) => ({ items: offset === 0 ? firstPage : offset === 50 ? secondPage : finalPage, total: 101, limit: 50, offset }));
    vi.spyOn(api, "reviewCandidates").mockImplementation(async (batchId) => {
      const ordinal = Number(batchId.split("-").at(-1));
      return { batch_id: batchId, batch_version: 1, total: 1, limit: 50, offset: 0, source_duplicate_evidence: [], items: [{ extraction_id: `extract-${ordinal}`, batch_id: batchId, candidate_ordinal: ordinal, candidate_key: String(ordinal).padStart(64, "0"), row_fingerprint: String(ordinal % 10).repeat(64), record_kind: "generic_document", financial_subtype: null, source_locator: `row/${ordinal}`, version: 1, status: "pending_review", payload: {}, field_confidences: {}, source_spans: {}, validation_issues: [], evidence_group_keys: [], latest_decision: null, duplicate_evidence: [] }] };
    });
    vi.spyOn(api, "intake").mockImplementation(async (intakeId) => {
      const ordinal = Number(intakeId.split("-").at(-1));
      return { intake_id: intakeId, document_id: `document-${ordinal}`, source_file_id: `source-${ordinal}`, source_version: 1, source_sha256: String(ordinal % 10).repeat(64), intake_intent: "generic_file", state: "processed", retryable: false, version: 1 };
    });
    vi.spyOn(api, "document").mockImplementation(async (documentId) => {
      const ordinal = Number(documentId.split("-").at(-1));
      return { id: documentId, doc_class: "other", status: "in_review", source_filename: `${documentId}.csv`, created_at: "2026-08-23T00:00:00Z", files: [{ id: `source-${ordinal}`, kind: "original", version: 1, source_filename: `${documentId}.csv`, mime: "text/csv", sha256: String(ordinal % 10).repeat(64) }] };
    });

    render(<I18nProvider><GroupedReviewWorkspace onReviewChanged={() => undefined} onEmpty={() => undefined} /></I18nProvider>);
    await screen.findByText("row/1");
    const enabledNext = () => screen.getAllByRole<HTMLButtonElement>("button", { name: "Next" }).find((button) => !button.disabled) as HTMLButtonElement;
    fireEvent.click(enabledNext());
    await waitFor(() => expect(reviewBatches).toHaveBeenCalledWith({ limit: 50, offset: 50 }));
    await screen.findByText("row/51");
    fireEvent.click(enabledNext());
    await waitFor(() => expect(reviewBatches).toHaveBeenCalledWith({ limit: 50, offset: 100 }));

    expect(await screen.findByText("row/101")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Source-bound batch" })).toHaveValue("batch-101");
    expect(screen.getByText("101–101 of 101")).toBeInTheDocument();
  });
});
