import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import OriginalPreview, { isCompletePdfPreview } from "@/components/original-preview";
import { I18nProvider } from "@/lib/i18n";

describe("OriginalPreview", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("keeps raw PDF attachment-only when no complete page manifest exists and binds the exact source", async () => {
    vi.spyOn(api, "pdfPreviewManifest").mockRejectedValue(new Error("unavailable"));
    vi.spyOn(api, "status").mockResolvedValue({
      id: "document-1", doc_class: "receipt", status: "in_review", source_filename: "receipt.pdf", created_at: "2026-08-17T00:00:00Z", files: [{ id: "source-4", kind: "original", version: 4, source_filename: "receipt.pdf", mime: "application/pdf", sha256: "a".repeat(64) }],
    });
    render(<I18nProvider><OriginalPreview item={{ document_id: "document-1", extraction_id: "extract-1", version: 2, source_file_id: "source-4", source_version: 4, doc_class: "receipt", flagged_fields: [], suggested: {}, source_spans: {}, suspected_duplicate_of: [], duplicate_candidates: [] }} /></I18nProvider>);
    await waitFor(() => expect(screen.getByText(/attachment-only/i)).toBeInTheDocument());
    expect(screen.queryByRole("img", { name: /PDF preview/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download preserved original" })).toHaveAttribute(
      "href",
      `/documents/document-1/original?version=4&source_file_id=source-4&sha256=${"a".repeat(64)}`,
    );
  });

  it("accepts only a complete ordered PNG manifest bound to the exact source", () => {
    const source = { document_id: "d", source_file_id: "s", source_version: 1, source_sha256: "b".repeat(64), filename: "a.pdf", mime: "application/pdf" };
    const manifest = { schema_version: 1, document_id: "d", source_file_id: "s", source_version: 1, source_sha256: "b".repeat(64), page_count: 2, status: "ready" as const, pages: [{ page_number: 2, artifact_id: "p2", sha256: "c".repeat(64), mime: "image/png" as const, width: 10, height: 10, byte_size: 10 }], manifest_sha256: "d".repeat(64), unavailable_reason: null };
    expect(isCompletePdfPreview(manifest, source)).toBe(false);
  });

  it("fails closed when status returns a different document identity", async () => {
    vi.spyOn(api, "status").mockResolvedValue({
      id: "document-other", doc_class: "receipt", status: "in_review", source_filename: "receipt.png", created_at: "2026-08-17T00:00:00Z", files: [{ id: "source-4", kind: "original", version: 4, source_filename: "receipt.png", mime: "image/png", sha256: "a".repeat(64) }],
    });
    const view = render(<I18nProvider><OriginalPreview item={{ document_id: "document-1", extraction_id: "extract-1", version: 2, source_file_id: "source-4", source_version: 4, doc_class: "receipt", flagged_fields: [], suggested: {}, source_spans: {}, suspected_duplicate_of: [], duplicate_candidates: [] }} /></I18nProvider>);
    const preview = within(view.container);

    await waitFor(() => expect(preview.getByText(/exact source identity is unavailable/i)).toBeInTheDocument());
    expect(preview.queryByRole("img")).not.toBeInTheDocument();
    expect(preview.queryByRole("link", { name: "Download preserved original" })).not.toBeInTheDocument();
  });

  it("falls back to attachment-only when a manifest page derivative cannot render", async () => {
    const source = { document_id: "document-1", source_file_id: "source-1", source_version: 1, source_sha256: "a".repeat(64), filename: "receipt.pdf", mime: "application/pdf" };
    vi.spyOn(api, "pdfPreviewManifest").mockResolvedValue({
      schema_version: 1,
      document_id: source.document_id,
      source_file_id: source.source_file_id,
      source_version: source.source_version,
      source_sha256: source.source_sha256,
      page_count: 1,
      status: "ready",
      pages: [{ page_number: 1, artifact_id: "page-1", sha256: "b".repeat(64), mime: "image/png", width: 100, height: 200, byte_size: 256 }],
      manifest_sha256: "c".repeat(64),
      unavailable_reason: null,
    });
    render(<I18nProvider><OriginalPreview source={source} /></I18nProvider>);

    const pageImage = await screen.findByRole("img", { name: /PDF preview page 1 of 1/i });
    fireEvent.error(pageImage);

    expect(screen.queryByRole("img", { name: /PDF preview/i })).not.toBeInTheDocument();
    expect(screen.getByText(/attachment-only/i)).toBeInTheDocument();
    expect(screen.getByText(/PDF page preview is unavailable/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download preserved original" })).toBeInTheDocument();
  });
});
