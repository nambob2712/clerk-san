import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { DocumentRecord, ReviewBatchSummary, SourceIntakeDetail } from "@/api/contracts";
import { api } from "@/api/client";
import DocumentsView from "@/features/documents/documents-view";
import { I18nProvider, useI18n } from "@/lib/i18n";

function documentRecord(id = "document-1", sourceFilename = "replacement.pdf", status = "verified"): DocumentRecord {
  return {
    id,
    doc_class: "receipt",
    status,
    source_filename: sourceFilename,
    created_at: "2026-08-17T00:00:00Z",
    files: [],
    verified: {},
  };
}

function documentPage(document: DocumentRecord): Awaited<ReturnType<typeof api.documents>> {
  return { items: [document], limit: 100, offset: 0 };
}

function intakeFor(documentId = "document-1", state: SourceIntakeDetail["state"] = "processed", ordinal = 1): SourceIntakeDetail {
  return {
    intake_id: `intake-${ordinal}`,
    document_id: documentId,
    source_file_id: `source-${ordinal}`,
    source_version: ordinal,
    source_sha256: String(ordinal % 10).repeat(64),
    intake_intent: "generic_file",
    state,
    retryable: false,
    version: 1,
  };
}

function batchFor(documentId = "document-1", lifecycle: ReviewBatchSummary["lifecycle"] = "active", ordinal = 1): ReviewBatchSummary {
  return {
    id: `batch-${ordinal}`,
    document_id: documentId,
    source_intake_id: `intake-${ordinal}`,
    source_file_id: `source-${ordinal}`,
    source_version: ordinal,
    lifecycle,
    version: 1,
    candidate_count: 2,
    pending_count: 0,
    included_count: 2,
    excluded_count: 0,
    error_count: 0,
    exception_count: 0,
    reconciliation_counts: {},
    reconciliation_digest: "d".repeat(64),
    created_at: "2026-08-23T00:00:00Z",
    updated_at: "2026-08-23T00:00:00Z",
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

function LocaleSwitches(): React.ReactElement {
  const { setLocale } = useI18n();
  return <><button onClick={() => setLocale("vi")}>VI</button><button onClick={() => setLocale("ja")}>JA</button></>;
}

describe("DocumentsView", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("opens the original bound to the verified record rather than a newer pending extraction", async () => {
    const document = documentRecord();
    document.files = [
      { id: "source-1", kind: "original", version: 1, source_filename: "old.pdf", mime: "application/pdf", sha256: "a".repeat(64) },
      { id: "source-2", kind: "original", version: 2, source_filename: "replacement.pdf", mime: "application/pdf" },
    ];
    document.extracted = { source_file_id: "source-2", source_version: 2 };
    document.verified = { source_file_id: "source-1", source_version: 1 };
    vi.spyOn(api, "documents").mockResolvedValue(documentPage(document));
    vi.spyOn(api, "recentIntakes").mockResolvedValue([]);
    vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });

    render(<I18nProvider><DocumentsView /></I18nProvider>);

    const link = await screen.findByRole("link", { name: "old.pdf · v1" });
    expect(link).toHaveAttribute("href", `/documents/document-1/original?version=1&source_file_id=source-1&sha256=${"a".repeat(64)}`);
  });

  it("does not expose an unchecksummed verified-source download", async () => {
    const document = documentRecord();
    document.files = [{ id: "source-1", kind: "original", version: 1, source_filename: "old.pdf", mime: "application/pdf", sha256: "malformed" }];
    document.verified = { source_file_id: "source-1", source_version: 1 };
    vi.spyOn(api, "documents").mockResolvedValue(documentPage(document));
    vi.spyOn(api, "recentIntakes").mockResolvedValue([]);
    vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });

    render(<I18nProvider><DocumentsView /></I18nProvider>);

    expect(await screen.findByText("Exact checksum unavailable — download disabled")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "old.pdf · v1" })).not.toBeInTheDocument();
  });

  it("shows source-intake failure as unknown and retries that supplement independently", async () => {
    vi.spyOn(api, "documents").mockResolvedValue(documentPage(documentRecord()));
    const intakes = vi.spyOn(api, "recentIntakes")
      .mockRejectedValueOnce(new Error("intakes offline"))
      .mockResolvedValueOnce([intakeFor()]);
    vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });

    render(<I18nProvider><DocumentsView /></I18nProvider>);

    expect(await screen.findByText("Latest source-intake data is unavailable: intakes offline")).toBeInTheDocument();
    expect(screen.getByText("Source intake unknown — supplemental data unavailable")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry source intake status" }));
    expect(await screen.findByText("Processed")).toBeInTheDocument();
    expect(intakes).toHaveBeenCalledTimes(2);
  });

  it("does not call a failed batch supplement an empty authority and exposes a retry", async () => {
    vi.spyOn(api, "documents").mockResolvedValue(documentPage(documentRecord()));
    vi.spyOn(api, "recentIntakes").mockResolvedValue([]);
    const batches = vi.spyOn(api, "reviewBatches")
      .mockRejectedValueOnce(new Error("batches offline"))
      .mockResolvedValueOnce({ items: [], total: 0, limit: 100, offset: 0 });

    render(<I18nProvider><DocumentsView /></I18nProvider>);

    expect(await screen.findByText("Batch-authority data is unavailable: batches offline")).toBeInTheDocument();
    expect(screen.getByText("Batch authority unknown — supplemental data unavailable")).toBeInTheDocument();
    expect(screen.queryByText("No source batch")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry batch authority" }));
    expect(await screen.findByText("No source batch")).toBeInTheDocument();
    expect(batches).toHaveBeenCalledTimes(2);
  });

  it("does not infer absence for a document omitted from full bounded supplemental windows", async () => {
    vi.spyOn(api, "documents").mockResolvedValue(documentPage(documentRecord()));
    vi.spyOn(api, "recentIntakes").mockResolvedValue(Array.from({ length: 100 }, (_, index) => intakeFor(`other-${index}`, "processed", index + 1)));
    const batches = Array.from({ length: 100 }, (_, index) => batchFor(`other-${index}`, "active", index + 1));
    vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: batches, total: 100, limit: 100, offset: 0 });

    render(<I18nProvider><DocumentsView /></I18nProvider>);

    expect(await screen.findByText("Not available in the recent-intake window")).toBeInTheDocument();
    expect(screen.getByText("Not available in the recent-batch window")).toBeInTheDocument();
    expect(screen.queryByText("No source intake")).not.toBeInTheDocument();
    expect(screen.queryByText("No source batch")).not.toBeInTheDocument();
  });

  it("does not present a non-active partial-window match as batch authority when an active batch may be omitted", async () => {
    vi.spyOn(api, "documents").mockResolvedValue(documentPage(documentRecord()));
    vi.spyOn(api, "recentIntakes").mockResolvedValue([]);
    const visible = [batchFor("document-1", "ready_to_activate", 2), ...Array.from({ length: 99 }, (_, index) => batchFor(`other-${index}`, "open", index + 3))];
    vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: visible, total: 101, limit: 100, offset: 0 });

    render(<I18nProvider><DocumentsView /></I18nProvider>);

    expect(await screen.findByText("Not available in the recent-batch window")).toBeInTheDocument();
    expect(screen.queryByText("Ready to activate")).not.toBeInTheDocument();
    expect(screen.queryByText("Source v2 · batch batch-2")).not.toBeInTheDocument();
  });

  it("shows that an active authority belongs to an older source than the latest intake", async () => {
    vi.spyOn(api, "documents").mockResolvedValue(documentPage(documentRecord()));
    vi.spyOn(api, "recentIntakes").mockResolvedValue([intakeFor("document-1", "processed", 2)]);
    vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: [batchFor("document-1", "active", 1)], total: 1, limit: 100, offset: 0 });

    render(<I18nProvider><DocumentsView /></I18nProvider>);

    expect(await screen.findByText("v2 · intake-2")).toBeInTheDocument();
    expect(screen.getByText("Source v1 · batch batch-1")).toBeInTheDocument();
  });

  it("localizes canonical document, intake, and batch states without exposing raw enums", async () => {
    vi.spyOn(api, "documents").mockResolvedValue(documentPage(documentRecord("document-1", "source.csv", "in_review")));
    vi.spyOn(api, "recentIntakes").mockResolvedValue([intakeFor("document-1", "stored_unprocessed")]);
    vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: [batchFor("document-1", "ready_to_activate")], total: 1, limit: 100, offset: 0 });

    render(<I18nProvider><LocaleSwitches /><DocumentsView /></I18nProvider>);

    expect(await screen.findByText("Preserved — processing unavailable")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "VI" }));
    expect(screen.getByText("Đã lưu — chưa thể xử lý")).toBeInTheDocument();
    expect(screen.getByText("Sẵn sàng kích hoạt")).toBeInTheDocument();
    expect(screen.getByText("Đang duyệt")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "JA" }));
    expect(screen.getByText("保存済み・処理不可")).toBeInTheDocument();
    expect(screen.getByText("有効化可能")).toBeInTheDocument();
    expect(screen.getByText("レビュー中")).toBeInTheDocument();
    expect(screen.queryByText("stored_unprocessed")).not.toBeInTheDocument();
    expect(screen.queryByText("ready_to_activate")).not.toBeInTheDocument();
    expect(screen.queryByText("in_review")).not.toBeInTheDocument();
  });

  it("applies filters only on submit and ignores an older request that finishes last", async () => {
    const first = deferred<Awaited<ReturnType<typeof api.documents>>>();
    const documents = vi.spyOn(api, "documents")
      .mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce(documentPage(documentRecord("document-new", "new.pdf")));
    vi.spyOn(api, "recentIntakes").mockResolvedValue([]);
    vi.spyOn(api, "reviewBatches").mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });

    render(<I18nProvider><DocumentsView /></I18nProvider>);
    await waitFor(() => expect(documents).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByLabelText("Counterparty"), { target: { value: "new vendor" } });
    expect(documents).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));

    expect(await screen.findByText("new.pdf")).toBeInTheDocument();
    expect(documents).toHaveBeenCalledTimes(2);
    expect(documents.mock.calls[1]?.[0]).toMatchObject({ counterparty: "new vendor", status: "verified" });
    act(() => { first.resolve(documentPage(documentRecord("document-old", "old.pdf"))); });
    await waitFor(() => expect(screen.queryByText("old.pdf")).not.toBeInTheDocument());
    expect(screen.getByText("new.pdf")).toBeInTheDocument();
  });
});
