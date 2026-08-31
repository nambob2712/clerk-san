import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import type { ReviewItem } from "@/api/contracts";
import ReviewWorkspace from "@/features/review/review-workspace";
import { I18nProvider } from "@/lib/i18n";

const reviewItem: ReviewItem = {
  document_id: "document-1",
  extraction_id: "extraction-1",
  version: 2,
  source_file_id: "source-1",
  source_version: 1,
  doc_class: "receipt",
  flagged_fields: [],
  suggested: { counterparty: { value: "Local shop", confidence: 0.9 } },
  source_spans: {},
  suspected_duplicate_of: [],
  duplicate_candidates: [],
};
const secondReviewItem: ReviewItem = {
  ...reviewItem,
  document_id: "document-2",
  extraction_id: "extraction-2",
  source_file_id: "source-2",
  source_version: 2,
  suggested: { counterparty: { value: "Second shop", confidence: 0.9 } },
};

const recoveryStorageKey = "clerksan.review.reprocess-recovery.v1";
let storage: Storage;

function memoryStorage(): Storage {
  const entries = new Map<string, string>();
  return {
    get length() { return entries.size; },
    clear: () => entries.clear(),
    getItem: (key) => entries.get(key) ?? null,
    key: (index) => Array.from(entries.keys())[index] ?? null,
    removeItem: (key) => { entries.delete(key); },
    setItem: (key, value) => { entries.set(key, value); },
  };
}

function prepareLegacyReview() {
  let rejected = false;
  vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });
  vi.spyOn(api, "pendingReview").mockImplementation(async () => rejected ? [] : [reviewItem]);
  vi.spyOn(api, "status").mockImplementation(async () => ({
    id: reviewItem.document_id,
    doc_class: reviewItem.doc_class,
    status: rejected ? "needs_reprocess" : "in_review",
    source_filename: "receipt.png",
    created_at: "2026-08-23T00:00:00Z",
    files: [{
      id: reviewItem.source_file_id,
      kind: "original",
      version: reviewItem.source_version,
      source_filename: "receipt.png",
      mime: "image/png",
      sha256: "a".repeat(64),
    }],
  }));
  const reject = vi.spyOn(api, "reject").mockImplementation(async () => { rejected = true; });
  const onReviewChanged = vi.fn();
  const { unmount } = render(<I18nProvider><ReviewWorkspace onReviewChanged={onReviewChanged} /></I18nProvider>);
  return { reject, onReviewChanged, unmount };
}

async function submitRejection(): Promise<void> {
  fireEvent.click(await screen.findByRole("button", { name: "Reject extraction and reprocess source" }));
  fireEvent.change(screen.getByLabelText("Rejection reason"), { target: { value: "incorrect extraction" } });
  fireEvent.click(screen.getByLabelText("I confirm this extraction should be rejected and reprocessed."));
  fireEvent.click(screen.getByRole("button", { name: "Reject and reprocess" }));
}

describe("legacy review rejection recovery", () => {
  beforeEach(() => {
    storage = memoryStorage();
    Object.defineProperty(window, "localStorage", { configurable: true, value: storage });
  });
  afterEach(() => { cleanup(); storage.clear(); vi.restoreAllMocks(); });

  it("retries only reprocessing after rejection committed and the first queue request failed", async () => {
    const { reject, onReviewChanged } = prepareLegacyReview();
    const reprocess = vi.spyOn(api, "reprocess")
      .mockRejectedValueOnce(new Error("worker unavailable"))
      .mockResolvedValueOnce({ document_id: reviewItem.document_id, original_version: 1, status: "queued", job_id: "job-1" });

    await submitRejection();

    expect(await screen.findByText("Rejected")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Rejected — Reprocessing could not be queued.");
    expect(screen.getByText("worker unavailable")).toBeInTheDocument();
    expect(reject).toHaveBeenCalledTimes(1);
    expect(reject).toHaveBeenCalledWith("extraction-1", "incorrect extraction", "local-user");
    expect(reprocess).toHaveBeenCalledTimes(1);
    expect(onReviewChanged).not.toHaveBeenCalled();
    expect(JSON.parse(storage.getItem(recoveryStorageKey) ?? "null")).toEqual({
      version: 1,
      document_id: reviewItem.document_id,
    });

    fireEvent.click(screen.getByRole("button", { name: "Reprocess source" }));

    await waitFor(() => expect(reprocess).toHaveBeenCalledTimes(2));
    expect(reprocess).toHaveBeenLastCalledWith("document-1", "local-user");
    expect(reject).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Rejected and queued for reprocessing.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(storage.getItem(recoveryStorageKey)).toBeNull();
    expect(onReviewChanged).toHaveBeenCalledTimes(1);
  });

  it("restores the document-bound retry after unmount and never repeats rejection", async () => {
    const { reject, onReviewChanged, unmount } = prepareLegacyReview();
    const reprocess = vi.spyOn(api, "reprocess")
      .mockRejectedValueOnce(new Error("worker unavailable"))
      .mockResolvedValueOnce({ document_id: reviewItem.document_id, original_version: 1, status: "queued", job_id: "job-1" });

    await submitRejection();
    expect(await screen.findByRole("alert")).toHaveTextContent("Rejected — Reprocessing could not be queued.");

    unmount();
    render(<I18nProvider><ReviewWorkspace onReviewChanged={onReviewChanged} /></I18nProvider>);

    expect(await screen.findByRole("alert")).toHaveTextContent("Rejected — Reprocessing could not be queued.");
    const retry = screen.getByRole("button", { name: "Reprocess source" });
    await waitFor(() => expect(retry).toBeEnabled());
    fireEvent.click(retry);

    await waitFor(() => expect(reprocess).toHaveBeenCalledTimes(2));
    expect(reprocess).toHaveBeenLastCalledWith(reviewItem.document_id, "local-user");
    expect(reject).toHaveBeenCalledTimes(1);
    expect(storage.getItem(recoveryStorageKey)).toBeNull();
    expect(onReviewChanged).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["malformed JSON", "{"],
    ["an incompatible version", JSON.stringify({ version: 2, document_id: reviewItem.document_id })],
  ])("fails closed for %s in recovery storage", async (_case, stored) => {
    storage.setItem(recoveryStorageKey, stored);
    vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });
    vi.spyOn(api, "pendingReview").mockResolvedValue([]);
    const reprocess = vi.spyOn(api, "reprocess").mockResolvedValue({
      document_id: reviewItem.document_id,
      original_version: 1,
      status: "queued",
      job_id: "job-1",
    });

    render(<I18nProvider><ReviewWorkspace onReviewChanged={() => undefined} /></I18nProvider>);

    expect(await screen.findByText("Your review queue is clear.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reprocess source" })).not.toBeInTheDocument();
    expect(reprocess).not.toHaveBeenCalled();
    expect(storage.getItem(recoveryStorageKey)).toBeNull();
  });

  it("clears a restored recovery only when exact server state proves it has progressed", async () => {
    storage.setItem(recoveryStorageKey, JSON.stringify({ version: 1, document_id: reviewItem.document_id }));
    const reviewBatches = vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });
    vi.spyOn(api, "pendingReview").mockResolvedValue([]);
    vi.spyOn(api, "status").mockResolvedValue({
      id: reviewItem.document_id,
      doc_class: reviewItem.doc_class,
      status: "in_review",
      source_filename: "receipt.png",
      created_at: "2026-08-23T00:00:00Z",
      files: [],
    });
    const reprocess = vi.spyOn(api, "reprocess").mockResolvedValue({
      document_id: reviewItem.document_id,
      original_version: 1,
      status: "queued",
      job_id: "job-1",
    });

    render(<I18nProvider><ReviewWorkspace onReviewChanged={() => undefined} /></I18nProvider>);

    await waitFor(() => expect(storage.getItem(recoveryStorageKey)).toBeNull());
    await waitFor(() => expect(reviewBatches).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(reprocess).not.toHaveBeenCalled();
  });

  it("preserves but does not execute a restored recovery when server status is unavailable", async () => {
    const stored = JSON.stringify({ version: 1, document_id: reviewItem.document_id });
    storage.setItem(recoveryStorageKey, stored);
    vi.spyOn(api, "pendingReview").mockResolvedValue([]);
    vi.spyOn(api, "status").mockRejectedValue(new Error("status unavailable"));
    const reprocess = vi.spyOn(api, "reprocess").mockResolvedValue({
      document_id: reviewItem.document_id,
      original_version: 1,
      status: "queued",
      job_id: "job-1",
    });

    render(<I18nProvider><ReviewWorkspace onReviewChanged={() => undefined} /></I18nProvider>);

    expect(await screen.findByText("status unavailable")).toBeInTheDocument();
    expect(storage.getItem(recoveryStorageKey)).toBe(stored);
    expect(reprocess).not.toHaveBeenCalled();
  });

  it("preserves the normal reject-and-reprocess success path", async () => {
    const { reject, onReviewChanged } = prepareLegacyReview();
    const reprocess = vi.spyOn(api, "reprocess").mockResolvedValue({
      document_id: reviewItem.document_id,
      original_version: 1,
      status: "queued",
      job_id: "job-1",
    });

    await submitRejection();

    expect(await screen.findByText("Rejected and queued for reprocessing.")).toBeInTheDocument();
    expect(reject).toHaveBeenCalledTimes(1);
    expect(reprocess).toHaveBeenCalledTimes(1);
    expect(storage.getItem(recoveryStorageKey)).toBeNull();
    expect(onReviewChanged).toHaveBeenCalledTimes(1);
  });
});

describe("legacy review exact-source preview", () => {
  beforeEach(() => {
    storage = memoryStorage();
    Object.defineProperty(window, "localStorage", { configurable: true, value: storage });
  });
  afterEach(() => { cleanup(); storage.clear(); vi.restoreAllMocks(); });

  it("removes the previous exact-source href before starting the newly selected status request", async () => {
    let resolveSecondStatus!: (value: Awaited<ReturnType<typeof api.status>>) => void;
    const secondStatus = new Promise<Awaited<ReturnType<typeof api.status>>>((resolve) => { resolveSecondStatus = resolve; });
    let previousHrefVisibleWhenSecondRequestStarted: boolean | undefined;
    vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });
    vi.spyOn(api, "pendingReview").mockResolvedValue([reviewItem, secondReviewItem]);
    vi.spyOn(api, "status").mockImplementation((documentId) => {
      if (documentId === reviewItem.document_id) {
        return Promise.resolve({
          id: reviewItem.document_id,
          doc_class: reviewItem.doc_class,
          status: "in_review",
          source_filename: "first.png",
          created_at: "2026-08-23T00:00:00Z",
          files: [{ id: reviewItem.source_file_id, kind: "original", version: reviewItem.source_version, source_filename: "first.png", mime: "image/png", sha256: "a".repeat(64) }],
        });
      }
      previousHrefVisibleWhenSecondRequestStarted = screen.queryByRole("link", { name: "Download preserved original" })
        ?.getAttribute("href")
        ?.includes(reviewItem.document_id) ?? false;
      return secondStatus;
    });

    render(<I18nProvider><ReviewWorkspace onReviewChanged={() => undefined} /></I18nProvider>);
    const firstLink = await screen.findByRole("link", { name: "Download preserved original" });
    expect(firstLink).toHaveAttribute("href", expect.stringContaining(reviewItem.document_id));

    fireEvent.change(screen.getByLabelText("Review document"), { target: { value: secondReviewItem.extraction_id } });

    expect(previousHrefVisibleWhenSecondRequestStarted).toBe(false);
    expect(screen.queryByRole("link", { name: "Download preserved original" })).not.toBeInTheDocument();

    resolveSecondStatus({
      id: secondReviewItem.document_id,
      doc_class: secondReviewItem.doc_class,
      status: "in_review",
      source_filename: "second.png",
      created_at: "2026-08-23T00:00:00Z",
      files: [{ id: secondReviewItem.source_file_id, kind: "original", version: secondReviewItem.source_version, source_filename: "second.png", mime: "image/png", sha256: "b".repeat(64) }],
    });
    expect(await screen.findByRole("link", { name: "Download preserved original" })).toHaveAttribute(
      "href",
      expect.stringContaining(secondReviewItem.document_id),
    );
  });
});

describe("legacy review mode selection", () => {
  beforeEach(() => {
    storage = memoryStorage();
    Object.defineProperty(window, "localStorage", { configurable: true, value: storage });
  });
  afterEach(() => { cleanup(); storage.clear(); vi.restoreAllMocks(); });

  it("re-probes grouped batches before reload and exposes no legacy mutation during a background arrival", async () => {
    const batch = {
      id: "batch-1",
      document_id: "document-batch",
      source_intake_id: "intake-batch",
      source_file_id: "source-batch",
      source_version: 1,
      lifecycle: "open" as const,
      version: 1,
      candidate_count: 1,
      pending_count: 1,
      included_count: 0,
      excluded_count: 0,
      error_count: 0,
      exception_count: 0,
      reconciliation_counts: { mapped_candidate: 1 },
      reconciliation_digest: "c".repeat(64),
      created_at: "2026-08-23T00:00:00Z",
      updated_at: "2026-08-23T00:00:00Z",
    };
    const batchPage = { items: [batch], total: 1, limit: 1, offset: 0 };
    let resolveArrival!: (value: typeof batchPage) => void;
    const arrival = new Promise<typeof batchPage>((resolve) => { resolveArrival = resolve; });
    vi.spyOn(api, "reviewBatches")
      .mockResolvedValueOnce({ items: [], total: 0, limit: 100, offset: 0 })
      .mockReturnValueOnce(arrival)
      .mockResolvedValue(batchPage);
    const pendingReview = vi.spyOn(api, "pendingReview").mockResolvedValue([reviewItem]);
    vi.spyOn(api, "status").mockResolvedValue({
      id: reviewItem.document_id,
      doc_class: reviewItem.doc_class,
      status: "in_review",
      source_filename: "receipt.png",
      created_at: "2026-08-23T00:00:00Z",
      files: [{ id: reviewItem.source_file_id, kind: "original", version: reviewItem.source_version, source_filename: "receipt.png", mime: "image/png", sha256: "a".repeat(64) }],
    });
    vi.spyOn(api, "reviewCandidates").mockResolvedValue({
      batch_id: batch.id,
      batch_version: batch.version,
      items: [],
      source_duplicate_evidence: [],
      total: 0,
      limit: 50,
      offset: 0,
    });
    vi.spyOn(api, "intake").mockResolvedValue({
      intake_id: batch.source_intake_id,
      document_id: batch.document_id,
      source_file_id: batch.source_file_id,
      source_version: batch.source_version,
      source_sha256: "b".repeat(64),
      intake_intent: "generic_file",
      state: "processed",
      retryable: false,
      version: 1,
    });
    vi.spyOn(api, "document").mockResolvedValue({
      id: batch.document_id,
      doc_class: "other",
      status: "in_review",
      source_filename: "records.csv",
      created_at: "2026-08-23T00:00:00Z",
      files: [{ id: batch.source_file_id, kind: "original", version: batch.source_version, source_filename: "records.csv", mime: "text/csv", sha256: "b".repeat(64) }],
    });
    const approve = vi.spyOn(api, "approve");
    const reject = vi.spyOn(api, "reject");
    const reprocess = vi.spyOn(api, "reprocess");

    render(<I18nProvider><ReviewWorkspace onReviewChanged={() => undefined} /></I18nProvider>);
    const legacyReject = await screen.findByRole("button", { name: "Reject extraction and reprocess source" });

    fireEvent.click(screen.getByRole("button", { name: "Reload queue" }));

    expect(screen.getByRole("button", { name: "Approve verified record" })).toBeDisabled();
    expect(legacyReject).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reprocess source" })).toBeDisabled();
    fireEvent.click(legacyReject);
    expect(screen.queryByLabelText("Rejection reason")).not.toBeInTheDocument();

    act(() => { resolveArrival(batchPage); });
    expect(await screen.findByRole("heading", { name: "Grouped source review" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve verified record" })).not.toBeInTheDocument();
    expect(pendingReview).toHaveBeenCalledTimes(1);
    expect(approve).not.toHaveBeenCalled();
    expect(reject).not.toHaveBeenCalled();
    expect(reprocess).not.toHaveBeenCalled();
  });
});
