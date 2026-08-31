import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, LocalApiError } from "@/api/client";
import App from "@/app";

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

describe("Workspace availability", () => {
  afterEach(() => {
    cleanup();
    window.location.hash = "";
    vi.restoreAllMocks();
  });

  it("explains a local data upgrade without exposing a generic HTTP 500", async () => {
    vi.spyOn(api, "health").mockResolvedValue({ status: "ok" });
    vi.spyOn(api, "ready").mockRejectedValue(new LocalApiError(503, { code: "local_data_needs_upgrade", message: "schema failed" }));
    vi.spyOn(api, "pendingReview").mockResolvedValue([]);
    vi.spyOn(api, "recentIntakes").mockResolvedValue([]);

    render(<App />);

    await waitFor(() => expect(screen.getAllByText(/reachable but not ready/i)).toHaveLength(2));
    expect(screen.getByText(/fix the local database or models/i)).toBeInTheDocument();
    expect(screen.queryByText(/Local API returned HTTP 500/i)).not.toBeInTheDocument();
  });

  it("keeps the legacy workspace available while processing models are delayed", async () => {
    vi.spyOn(api, "health").mockResolvedValue({ status: "ok" });
    vi.spyOn(api, "ready").mockResolvedValue({
      status: "ready",
      intake_ready: true,
      review_ready: true,
      processing_ready: false,
      universal_processing_ready: false,
      processing_reason_codes: ["model_unavailable"],
    });
    vi.spyOn(api, "capabilities").mockResolvedValue({ schema: "clerksan.capabilities", version: 1, process: [], sandbox_verified: false, registry_digest: "r", capabilities_digest: "c" });
    vi.spyOn(api, "pendingReview").mockResolvedValue([]);
    vi.spyOn(api, "recentIntakes").mockResolvedValue([]);

    render(<App />);

    await waitFor(() => expect(screen.getByText("Local service ready")).toBeInTheDocument());
    expect(screen.queryByText(/reachable but not ready/i)).not.toBeInTheDocument();
    expect(screen.getByText(/Your review queue is clear/i)).toBeInTheDocument();
  });

  it("keeps intake enabled and explains delayed processing when only models are unavailable", async () => {
    window.location.hash = "intake";
    vi.spyOn(api, "health").mockResolvedValue({ status: "ok" });
    vi.spyOn(api, "ready").mockResolvedValue({
      status: "not_ready",
      intake_ready: true,
      review_ready: true,
      processing_ready: false,
      universal_processing_ready: false,
      processing_reason_codes: ["model_unavailable"],
    });
    vi.spyOn(api, "capabilities").mockResolvedValue({ schema: "clerksan.capabilities", version: 1, process: [], sandbox_verified: false, registry_digest: "r", capabilities_digest: "c" });
    vi.spyOn(api, "recentIntakes").mockResolvedValue([]);

    render(<App />);

    expect(await screen.findByText(/background processing is delayed/i)).toBeInTheDocument();
    expect(screen.getByText(/Broad file processing is not ready/i)).toBeInTheDocument();
    expect(screen.getByText(/CSV and other broad formats require the isolated parser/i)).toBeInTheDocument();
    expect(screen.getByLabelText("Upload file")).toBeEnabled();
    expect(screen.getByLabelText("Scan bill")).toBeEnabled();
  });

  it("does not advertise CSV unless both CSV and XLSX are explicitly available", async () => {
    window.location.hash = "intake";
    vi.spyOn(api, "health").mockResolvedValue({ status: "ok" });
    vi.spyOn(api, "ready").mockResolvedValue({
      status: "ready",
      intake_ready: true,
      review_ready: true,
      processing_ready: true,
      universal_processing_ready: true,
    });
    vi.spyOn(api, "capabilities").mockResolvedValue({ schema: "clerksan.capabilities", version: 1, process: ["pdf", "jpeg", "png", "webp", "docx", "xlsx", "md"], sandbox_verified: true, registry_digest: "r", capabilities_digest: "c" });
    vi.spyOn(api, "recentIntakes").mockResolvedValue([]);

    render(<App />);

    expect(await screen.findByText(/Broad file processing is not ready/i)).toBeInTheDocument();
    expect(screen.getByText(/CSV and other broad formats require the isolated parser/i)).toBeInTheDocument();
    expect(screen.queryByText(/Upload CSV, XLSX, DOCX, PDF/i)).not.toBeInTheDocument();
  });

  it("advertises broad uploads when CSV and XLSX are both explicitly available", async () => {
    window.location.hash = "intake";
    vi.spyOn(api, "health").mockResolvedValue({ status: "ok" });
    vi.spyOn(api, "ready").mockResolvedValue({
      status: "ready",
      intake_ready: true,
      review_ready: true,
      processing_ready: true,
      universal_processing_ready: true,
    });
    vi.spyOn(api, "capabilities").mockResolvedValue({ schema: "clerksan.capabilities", version: 1, process: ["csv", "xlsx"], sandbox_verified: true, registry_digest: "r", capabilities_digest: "c" });
    vi.spyOn(api, "recentIntakes").mockResolvedValue([]);

    render(<App />);

    expect(await screen.findByText(/including CSV or XLSX/i)).toBeInTheDocument();
    expect(screen.queryByText(/Broad file processing is not ready/i)).not.toBeInTheDocument();
  });

  it("keeps the in-flight availability request stable across locale changes", async () => {
    let resolveHealth: ((value: { status: "ok" }) => void) | undefined;
    const health = vi.spyOn(api, "health").mockImplementation(() => new Promise((resolve) => { resolveHealth = resolve; }));
    const ready = vi.spyOn(api, "ready").mockResolvedValue({ status: "ready", intake_ready: true, review_ready: true, processing_ready: true, universal_processing_ready: true });
    const capabilities = vi.spyOn(api, "capabilities").mockResolvedValue({ schema: "clerksan.capabilities", version: 1, process: ["csv", "xlsx"], sandbox_verified: true, registry_digest: "r", capabilities_digest: "c" });
    vi.spyOn(api, "pendingReview").mockResolvedValue([]);
    vi.spyOn(api, "recentIntakes").mockResolvedValue([]);

    render(<App />);
    await waitFor(() => expect(health).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "vi" } });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "ja" } });
    expect(health).toHaveBeenCalledTimes(1);

    await act(async () => { resolveHealth?.({ status: "ok" }); });
    await waitFor(() => expect(screen.getByText("ローカルサービスの準備ができました")).toBeInTheDocument());
    expect(ready).toHaveBeenCalledTimes(1);
    expect(capabilities).toHaveBeenCalledTimes(1);
  });

  it("rebinds mapping to the complete hash identity when navigating A to B and back", async () => {
    window.location.hash = "mapping/document%20A";
    vi.spyOn(api, "health").mockResolvedValue({ status: "ok" });
    vi.spyOn(api, "ready").mockResolvedValue({ status: "ready", intake_ready: true, processing_ready: true, universal_processing_ready: true });
    vi.spyOn(api, "capabilities").mockResolvedValue({ schema: "clerksan.capabilities", version: 1, process: ["csv", "xlsx"], sandbox_verified: true, registry_digest: "r", capabilities_digest: "c" });
    vi.spyOn(api, "recentIntakes").mockResolvedValue([]);
    const sourceFor = (documentId: string) => ({ source_intake_id: `intake-${documentId}`, source_file_id: `source-${documentId}`, source_version: 1, source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64) });
    const descriptors = vi.spyOn(api, "schemaDescriptors").mockImplementation(async (documentId) => ({ document_id: documentId, source: sourceFor(documentId), descriptors: [] }));
    vi.spyOn(api, "mappings").mockImplementation(async (documentId) => ({ document_id: documentId, source: sourceFor(documentId), items: [] }));

    render(<App />);
    await waitFor(() => expect(descriptors).toHaveBeenCalledWith("document A"));

    act(() => { window.location.hash = "mapping/document%20B"; });
    await waitFor(() => expect(descriptors).toHaveBeenCalledWith("document B"));

    act(() => { window.history.back(); });
    await waitFor(() => expect(window.location.hash).toBe("#mapping/document%20A"));
    await waitFor(() => expect(descriptors.mock.calls.filter(([documentId]) => documentId === "document A")).toHaveLength(2));
  });

  it.each(["#mapping", "#mapping/%E0%A4%A"])("normalizes invalid mapping location %s to Intake", async (hash) => {
    window.history.replaceState(window.history.state, "", hash);
    vi.spyOn(api, "health").mockResolvedValue({ status: "ok" });
    vi.spyOn(api, "ready").mockResolvedValue({ status: "ready", intake_ready: true, processing_ready: true, universal_processing_ready: true });
    vi.spyOn(api, "capabilities").mockResolvedValue({ schema: "clerksan.capabilities", version: 1, process: ["csv", "xlsx"], sandbox_verified: true, registry_digest: "r", capabilities_digest: "c" });
    vi.spyOn(api, "recentIntakes").mockResolvedValue([]);

    render(<App />);

    await waitFor(() => expect(window.location.hash).toBe("#intake"));
    expect(screen.getByRole("button", { name: "Intake" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("heading", { name: "Add documents" })).toBeInTheDocument();
  });

  it("locks shell and hash navigation for the complete delayed mapping apply", async () => {
    window.history.replaceState(window.history.state, "", "#intake");
    window.history.pushState(window.history.state, "", "#mapping/document-1");
    const source = { source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64) };
    const descriptor = { table_locator: "transactions", ordered_headers: ["memo"], inferred_types: ["string"], row_count: 1, schema_fingerprint: "d".repeat(64) };
    vi.spyOn(api, "health").mockResolvedValue({ status: "ok" });
    vi.spyOn(api, "ready").mockResolvedValue({ status: "ready", intake_ready: true, processing_ready: true, universal_processing_ready: true });
    vi.spyOn(api, "capabilities").mockResolvedValue({ schema: "clerksan.capabilities", version: 1, process: ["csv", "xlsx"], sandbox_verified: true, registry_digest: "r", capabilities_digest: "c" });
    vi.spyOn(api, "schemaDescriptors").mockResolvedValue({ document_id: "document-1", source, descriptors: [descriptor] });
    vi.spyOn(api, "mappings").mockResolvedValue({ document_id: "document-1", source, items: [] });
    vi.spyOn(api, "createMapping").mockImplementation(async (_documentId, body) => ({ ...body, id: "mapping-1", mapping_version: 1, mapping_digest: "f".repeat(64), created_at: "2026-08-23T00:00:00Z" }));
    vi.spyOn(api, "previewMappingSet").mockResolvedValue({ document_id: "document-1", source, candidate_count: 1, reconciliation_counts: { mapped_candidate: 1 }, previews: [{ table_locator: "transactions", rows: [], total_rows: 1, valid_rows: 1, error_rows: 0, blank_rows: 0, truncated: false }] });
    const pendingSet = deferred<Awaited<ReturnType<typeof api.createMappingSet>>>();
    const createSet = vi.spyOn(api, "createMappingSet").mockReturnValue(pendingSet.promise);
    const applySet = vi.spyOn(api, "applyMappingSet").mockResolvedValue({ id: "batch-1", document_id: "document-1", source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, source_sha256: source.source_sha256, normalized_sha256: source.normalized_sha256, structure_fingerprint: source.structure_fingerprint, mapping_set_id: "set-1", mapping_set_version: 1, mapping_set_digest: "9".repeat(64), lifecycle: "open", candidate_count: 1, reconciliation_counts: { mapped_candidate: 1 }, reconciliation_digest: "8".repeat(64), version: 1, replayed: false });
    const reviewBatches = vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
    vi.spyOn(api, "pendingReview").mockResolvedValue([]);

    render(<App />);
    await screen.findByText("transactions");
    fireEvent.click(screen.getByRole("button", { name: "Save definitions and preview" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Create candidate batch" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Create candidate batch" }));
    await waitFor(() => expect(createSet).toHaveBeenCalledTimes(1));

    for (const name of ["Intake", "Review", "Documents", "Bills", "Search"]) expect(screen.getByRole("button", { name })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    expect(window.location.hash).toBe("#mapping/document-1");
    act(() => { window.history.back(); });
    await waitFor(() => expect(window.location.hash).toBe("#mapping/document-1"));
    act(() => { window.location.hash = "documents"; });
    await waitFor(() => expect(window.location.hash).toBe("#mapping/document-1"));
    expect(screen.getAllByText("transactions")).not.toHaveLength(0);

    await act(async () => {
      pendingSet.resolve({ id: "set-1", document_id: "document-1", source, set_digest: "9".repeat(64), version: 1, created_by: "local-user", created_at: "2026-08-23T00:00:00Z", entries: [{ table_locator: "transactions", schema_fingerprint: descriptor.schema_fingerprint, mapping_id: "mapping-1", mapping_version: 1, ordinal: 1 }] });
      await pendingSet.promise;
    });
    await waitFor(() => expect(window.location.hash).toBe("#review"));
    await waitFor(() => expect(reviewBatches).toHaveBeenCalledTimes(1));
    expect(applySet).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Review" })).toBeEnabled();
  });

  it("keeps the mounted review cohort and success notice after a review mutation notification", async () => {
    window.location.hash = "review";
    const makeBatch = (ordinal: number, lifecycle: "open" | "rejected" = "open") => ({ id: `batch-${ordinal}`, document_id: `document-${ordinal}`, source_intake_id: `intake-${ordinal}`, source_file_id: `source-${ordinal}`, source_version: 1, lifecycle, version: lifecycle === "open" ? 1 : 2, candidate_count: 1, pending_count: 1, included_count: 0, excluded_count: 0, error_count: 0, exception_count: 0, reconciliation_counts: { mapped_candidate: 1 }, reconciliation_digest: String(ordinal % 10).repeat(64), created_at: "2026-08-23T00:00:00Z", updated_at: "2026-08-23T00:00:00Z" });
    const firstPage = Array.from({ length: 50 }, (_, index) => makeBatch(index + 1));
    let reprocessed = false;
    vi.spyOn(api, "health").mockResolvedValue({ status: "ok" });
    vi.spyOn(api, "ready").mockResolvedValue({ status: "ready", intake_ready: true, review_ready: true, processing_ready: true, universal_processing_ready: true });
    vi.spyOn(api, "capabilities").mockResolvedValue({ schema: "clerksan.capabilities", version: 1, process: ["csv", "xlsx"], sandbox_verified: true, registry_digest: "r", capabilities_digest: "c" });
    vi.spyOn(api, "recentIntakes").mockResolvedValue([]);
    vi.spyOn(api, "pendingReview").mockResolvedValue([]);
    vi.spyOn(api, "reviewBatches").mockImplementation(async ({ offset = 0 } = {}) => ({ items: offset === 0 ? firstPage : [makeBatch(51, reprocessed ? "rejected" : "open")], total: 51, limit: 50, offset }));
    vi.spyOn(api, "reviewCandidates").mockImplementation(async (batchId) => {
      const ordinal = Number(batchId.split("-").at(-1));
      return { batch_id: batchId, batch_version: reprocessed && batchId === "batch-51" ? 2 : 1, total: 1, limit: 50, offset: 0, source_duplicate_evidence: [], items: [{ extraction_id: `extract-${ordinal}`, batch_id: batchId, candidate_ordinal: ordinal, candidate_key: String(ordinal).padStart(64, "0"), row_fingerprint: String(ordinal % 10).repeat(64), record_kind: "generic_document", financial_subtype: null, source_locator: `row/${ordinal}`, version: 1, status: "pending_review", payload: {}, field_confidences: {}, source_spans: {}, validation_issues: [], evidence_group_keys: [], latest_decision: null, duplicate_evidence: [] }] };
    });
    vi.spyOn(api, "intake").mockImplementation(async (intakeId) => {
      const ordinal = Number(intakeId.split("-").at(-1));
      return { intake_id: intakeId, document_id: `document-${ordinal}`, source_file_id: `source-${ordinal}`, source_version: 1, source_sha256: String(ordinal % 10).repeat(64), intake_intent: "generic_file", state: "processed", retryable: false, version: 1 };
    });
    vi.spyOn(api, "document").mockImplementation(async (documentId) => {
      const ordinal = Number(documentId.split("-").at(-1));
      return { id: documentId, doc_class: "other", status: "in_review", source_filename: `${documentId}.csv`, created_at: "2026-08-23T00:00:00Z", files: [{ id: `source-${ordinal}`, kind: "original", version: 1, source_filename: `${documentId}.csv`, mime: "text/csv", sha256: String(ordinal % 10).repeat(64) }] };
    });
    const rejectAndReprocess = vi.spyOn(api, "rejectAndReprocessBatch").mockImplementation(async () => {
      reprocessed = true;
      return { batch_id: "batch-51", document_id: "document-51", source_intake_id: "intake-51", source_file_id: "source-51", source_version: 1, batch_version: 2, lifecycle: "rejected", status: "queued", job_id: "job-51" };
    });

    render(<App />);
    await screen.findByText("row/1");
    const enabledNext = () => screen.getAllByRole<HTMLButtonElement>("button", { name: "Next" }).find((button) => !button.disabled) as HTMLButtonElement;
    fireEvent.click(enabledNext());
    expect(await screen.findByText("row/51")).toBeInTheDocument();
    const selector = screen.getByLabelText("Source-bound batch");
    fireEvent.change(screen.getByLabelText("Rejection reason"), { target: { value: "retry exact source" } });
    fireEvent.click(screen.getByLabelText("I confirm this complete source batch should be rejected and reprocessed."));
    fireEvent.click(screen.getByRole("button", { name: "Reject and reprocess" }));

    await waitFor(() => expect(rejectAndReprocess).toHaveBeenCalledWith("batch-51", 1, "retry exact source", "local-user"));
    expect(await screen.findByText(/queued for reprocessing/i)).toBeInTheDocument();
    expect(screen.getByLabelText("Source-bound batch")).toBe(selector);
    expect(selector).toHaveValue("batch-51");
    expect(screen.getByText("51–51 of 51")).toBeInTheDocument();
  });
});
