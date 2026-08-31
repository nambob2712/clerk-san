import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, LocalApiError } from "@/api/client";
import MappingWorkspace from "@/features/mapping/mapping-workspace";
import { I18nProvider, useI18n } from "@/lib/i18n";

function LocaleSwitches(): React.ReactElement {
  const { setLocale } = useI18n();
  return <><button type="button" onClick={() => setLocale("vi")}>VI</button><button type="button" onClick={() => setLocale("ja")}>JA</button></>;
}

describe("MappingWorkspace", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("creates a complete source mapping set and renders hostile-looking preview values literally", async () => {
    const source = { source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64) };
    const descriptors = [
      { table_locator: "transactions", ordered_headers: ["memo"], inferred_types: ["string"], row_count: 1, schema_fingerprint: "d".repeat(64) },
      { table_locator: "notes", ordered_headers: ["note"], inferred_types: ["string"], row_count: 1, schema_fingerprint: "e".repeat(64) },
    ];
    vi.spyOn(api, "schemaDescriptors").mockResolvedValue({ document_id: "document-1", source, descriptors });
    vi.spyOn(api, "mappings").mockResolvedValue({ document_id: "document-1", source, items: [] });
    vi.spyOn(api, "createMapping").mockImplementation(async (_documentId, body) => ({ ...body, id: `mapping-${body.table_locator}`, mapping_version: 1, mapping_digest: "f".repeat(64), created_at: "2026-08-23T00:00:00Z" }));
    const preview = vi.spyOn(api, "previewMappingSet").mockResolvedValue({ document_id: "document-1", source, candidate_count: 2, reconciliation_counts: { mapped_candidate: 2 }, previews: [{ table_locator: "transactions", rows: [{ row_ordinal: 1, source_locator: "transactions/1", values: { memo: "&lt;b&gt;literal&lt;/b&gt;" }, errors: [] }], total_rows: 1, valid_rows: 1, error_rows: 0, blank_rows: 0, truncated: false }] });

    render(<I18nProvider><MappingWorkspace documentId="document-1" onApplied={() => undefined} /></I18nProvider>);
    await screen.findByText("transactions");
    expect(screen.getByRole("button", { name: "Create candidate batch" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Save definitions and preview" }));

    await waitFor(() => expect(preview).toHaveBeenCalledTimes(1));
    expect(preview.mock.calls[0]?.[1].entries).toHaveLength(2);
    expect(screen.queryByText("literal", { selector: "b" })).not.toBeInTheDocument();
    expect(await screen.findByText(/&lt;b&gt;literal&lt;\/b&gt;/u)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create candidate batch" })).toBeEnabled();
  });

  it("ignores a preview response created from an older draft revision", async () => {
    const source = { source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64) };
    const descriptor = { table_locator: "transactions", ordered_headers: ["memo"], inferred_types: ["string"], row_count: 1, schema_fingerprint: "d".repeat(64) };
    let resolvePreview!: (value: Awaited<ReturnType<typeof api.previewMappingSet>>) => void;
    const delayedPreview = new Promise<Awaited<ReturnType<typeof api.previewMappingSet>>>((resolve) => { resolvePreview = resolve; });
    vi.spyOn(api, "schemaDescriptors").mockResolvedValue({ document_id: "document-1", source, descriptors: [descriptor] });
    vi.spyOn(api, "mappings").mockResolvedValue({ document_id: "document-1", source, items: [] });
    vi.spyOn(api, "createMapping").mockImplementation(async (_documentId, body) => ({ ...body, id: "mapping-1", mapping_version: 1, mapping_digest: "f".repeat(64), created_at: "2026-08-23T00:00:00Z" }));
    const preview = vi.spyOn(api, "previewMappingSet").mockReturnValue(delayedPreview);

    const { container } = render(<I18nProvider><MappingWorkspace documentId="document-1" onApplied={() => undefined} /></I18nProvider>);
    await screen.findByText("transactions");
    fireEvent.click(screen.getByRole("button", { name: "Save definitions and preview" }));
    await waitFor(() => expect(preview).toHaveBeenCalledTimes(1));
    const destination = container.querySelector<HTMLInputElement>('input[list="canonical-fields"]');
    expect(destination).not.toBeNull();
    fireEvent.change(destination as HTMLInputElement, { target: { value: "title" } });
    resolvePreview({ document_id: "document-1", source, candidate_count: 1, reconciliation_counts: { mapped_candidate: 1 }, previews: [{ table_locator: "transactions", rows: [], total_rows: 1, valid_rows: 1, error_rows: 0, blank_rows: 0, truncated: false }] });

    await waitFor(() => expect(screen.getByRole("button", { name: "Create candidate batch" })).toBeDisabled());
    expect(screen.queryByText("Escaped sample and reconciliation")).not.toBeInTheDocument();
  });

  it("fails closed when descriptor and saved-mapping responses refer to different exact sources", async () => {
    const source = { source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64) };
    const replacement = { ...source, source_intake_id: "intake-2", source_file_id: "source-2", source_version: 2, source_sha256: "9".repeat(64) };
    vi.spyOn(api, "schemaDescriptors").mockResolvedValue({ document_id: "document-1", source, descriptors: [{ table_locator: "transactions", ordered_headers: ["memo"], inferred_types: ["string"], row_count: 1, schema_fingerprint: "d".repeat(64) }] });
    vi.spyOn(api, "mappings").mockResolvedValue({ document_id: "document-1", source: replacement, items: [] });

    render(<I18nProvider><MappingWorkspace documentId="document-1" onApplied={() => undefined} /></I18nProvider>);

    expect(await screen.findByRole("alert")).toHaveTextContent("exact source or schema changed");
    expect(screen.queryByText("transactions")).not.toBeInTheDocument();
  });

  it("clears an incompatible saved selection when a stale reload returns a replacement schema", async () => {
    const source = { source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64) };
    const replacement = { ...source, source_intake_id: "intake-2", source_file_id: "source-2", source_version: 2, source_sha256: "9".repeat(64), structure_fingerprint: "8".repeat(64) };
    const originalDescriptor = { table_locator: "transactions", ordered_headers: ["memo"], inferred_types: ["string"], row_count: 1, schema_fingerprint: "d".repeat(64) };
    const replacementDescriptor = { ...originalDescriptor, ordered_headers: ["description"], schema_fingerprint: "e".repeat(64) };
    const saved = {
      source,
      idempotency_key: "mapping-key",
      table_locator: "transactions",
      schema_fingerprint: originalDescriptor.schema_fingerprint,
      record_kind: "generic_document" as const,
      financial_subtype: null,
      field_rules: [],
      required_fields: [],
      mapping_version: 1,
      created_by: "local-user",
      id: "mapping-1",
      mapping_digest: "f".repeat(64),
      created_at: "2026-08-23T00:00:00Z",
    };
    vi.spyOn(api, "schemaDescriptors")
      .mockResolvedValueOnce({ document_id: "document-1", source, descriptors: [originalDescriptor] })
      .mockResolvedValueOnce({ document_id: "document-1", source: replacement, descriptors: [replacementDescriptor] });
    vi.spyOn(api, "mappings")
      .mockResolvedValueOnce({ document_id: "document-1", source, items: [saved] })
      .mockResolvedValueOnce({ document_id: "document-1", source: replacement, items: [] });
    vi.spyOn(api, "previewMappingSet").mockRejectedValue(new LocalApiError(409, { code: "stale_mapping_source", message: "Reload exact source" }));

    render(<I18nProvider><MappingWorkspace documentId="document-1" onApplied={() => undefined} /></I18nProvider>);
    fireEvent.change(await screen.findByLabelText("Saved immutable definition"), { target: { value: "mapping-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Save definitions and preview" }));
    fireEvent.click(await screen.findByRole("button", { name: "Reload structure and keep compatible drafts" }));

    const destination = await screen.findByLabelText("Destination field for description");
    expect(destination).toHaveValue("description");
    expect(screen.queryByText("Using the saved immutable definition.")).not.toBeInTheDocument();
    expect(screen.getByText(/incompatible saved selection was cleared/i)).toBeInTheDocument();
  });

  it("keeps compatible draft edits when the user changes locale", async () => {
    const source = { source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64) };
    const descriptor = { table_locator: "transactions", ordered_headers: ["memo"], inferred_types: ["string"], row_count: 1, schema_fingerprint: "d".repeat(64) };
    const descriptors = vi.spyOn(api, "schemaDescriptors").mockResolvedValue({ document_id: "document-1", source, descriptors: [descriptor] });
    const mappings = vi.spyOn(api, "mappings").mockResolvedValue({ document_id: "document-1", source, items: [] });

    render(<I18nProvider><LocaleSwitches /><MappingWorkspace documentId="document-1" onApplied={() => undefined} /></I18nProvider>);
    const destination = await screen.findByLabelText("Destination field for memo");
    fireEvent.change(destination, { target: { value: "title" } });
    fireEvent.click(screen.getByRole("button", { name: "VI" }));

    expect(await screen.findByLabelText("Trường đích cho memo")).toHaveValue("title");
    expect(descriptors).toHaveBeenCalledTimes(1);
    expect(mappings).toHaveBeenCalledTimes(1);
  });

  it("localizes saved mapping record kinds while preserving mapping IDs", async () => {
    const source = { source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64) };
    const descriptor = { table_locator: "transactions", ordered_headers: ["memo"], inferred_types: ["string"], row_count: 1, schema_fingerprint: "d".repeat(64) };
    const savedGeneric = { id: "generic-map-1", source, idempotency_key: "generic-key", table_locator: descriptor.table_locator, schema_fingerprint: descriptor.schema_fingerprint, record_kind: "generic_document" as const, financial_subtype: null, field_rules: [], required_fields: [], mapping_version: 1, mapping_digest: "e".repeat(64), created_by: "reviewer", created_at: "2026-08-23T00:00:00Z" };
    const savedFinancial = { ...savedGeneric, id: "finance-map-2", idempotency_key: "financial-key", record_kind: "financial" as const, financial_subtype: "invoice" as const, mapping_version: 2, mapping_digest: "f".repeat(64) };
    vi.spyOn(api, "schemaDescriptors").mockResolvedValue({ document_id: "document-1", source, descriptors: [descriptor] });
    vi.spyOn(api, "mappings").mockResolvedValue({ document_id: "document-1", source, items: [savedGeneric, savedFinancial] });

    render(<I18nProvider><LocaleSwitches /><MappingWorkspace documentId="document-1" onApplied={() => undefined} /></I18nProvider>);
    await screen.findByLabelText("Saved immutable definition");
    fireEvent.click(screen.getByRole("button", { name: "VI" }));
    expect(screen.getByRole("option", { name: "Tài liệu chung · v1 · generic-" })).toHaveValue("generic-map-1");
    expect(screen.getByRole("option", { name: "Bản ghi tài chính · v2 · finance-" })).toHaveValue("finance-map-2");
    fireEvent.click(screen.getByRole("button", { name: "JA" }));
    expect(screen.getByRole("option", { name: "一般文書 · v1 · generic-" })).toHaveValue("generic-map-1");
    expect(screen.getByRole("option", { name: "財務記録 · v2 · finance-" })).toHaveValue("finance-map-2");
  });

  it("requires an explicit financial subtype and preserves the selected wire value", async () => {
    const source = { source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64) };
    const descriptor = { table_locator: "transactions", ordered_headers: ["memo"], inferred_types: ["string"], row_count: 1, schema_fingerprint: "d".repeat(64) };
    vi.spyOn(api, "schemaDescriptors").mockResolvedValue({ document_id: "document-1", source, descriptors: [descriptor] });
    vi.spyOn(api, "mappings").mockResolvedValue({ document_id: "document-1", source, items: [] });
    const createMapping = vi.spyOn(api, "createMapping").mockImplementation(async (_documentId, body) => ({ ...body, id: "mapping-1", mapping_version: 1, mapping_digest: "f".repeat(64), created_at: "2026-08-23T00:00:00Z" }));
    vi.spyOn(api, "previewMappingSet").mockResolvedValue({ document_id: "document-1", source, candidate_count: 1, reconciliation_counts: { mapped_candidate: 1 }, previews: [] });

    render(<I18nProvider><MappingWorkspace documentId="document-1" onApplied={() => undefined} /></I18nProvider>);
    fireEvent.change(await screen.findByLabelText("Record kind"), { target: { value: "financial" } });

    const subtype = screen.getByLabelText("Financial subtype");
    expect(subtype).toHaveValue("");
    expect(screen.getByRole("option", { name: "Select a financial subtype" })).toHaveValue("");
    expect(screen.getByRole("option", { name: "Receipt" })).toHaveValue("receipt");
    expect(screen.getByRole("button", { name: "Save definitions and preview" })).toBeDisabled();

    fireEvent.change(subtype, { target: { value: "receipt" } });
    fireEvent.click(screen.getByRole("button", { name: "Save definitions and preview" }));
    await waitFor(() => expect(createMapping).toHaveBeenCalledWith("document-1", expect.objectContaining({ record_kind: "financial", financial_subtype: "receipt" })));
  });

  it("allows an immutable saved financial mapping to retain its stored subtype", async () => {
    const source = { source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64) };
    const descriptor = { table_locator: "transactions", ordered_headers: ["memo"], inferred_types: ["string"], row_count: 1, schema_fingerprint: "d".repeat(64) };
    const saved = { id: "mapping-financial", source, idempotency_key: "saved-key", table_locator: "transactions", schema_fingerprint: descriptor.schema_fingerprint, record_kind: "financial" as const, financial_subtype: "invoice" as const, field_rules: [], required_fields: [], mapping_version: 3, mapping_digest: "f".repeat(64), created_by: "reviewer", created_at: "2026-08-23T00:00:00Z" };
    vi.spyOn(api, "schemaDescriptors").mockResolvedValue({ document_id: "document-1", source, descriptors: [descriptor] });
    vi.spyOn(api, "mappings").mockResolvedValue({ document_id: "document-1", source, items: [saved] });
    const createMapping = vi.spyOn(api, "createMapping");
    const preview = vi.spyOn(api, "previewMappingSet").mockResolvedValue({ document_id: "document-1", source, candidate_count: 1, reconciliation_counts: { mapped_candidate: 1 }, previews: [] });

    render(<I18nProvider><MappingWorkspace documentId="document-1" onApplied={() => undefined} /></I18nProvider>);
    fireEvent.change(await screen.findByLabelText("Saved immutable definition"), { target: { value: "mapping-financial" } });
    fireEvent.click(screen.getByRole("button", { name: "Save definitions and preview" }));

    await waitFor(() => expect(preview).toHaveBeenCalledWith("document-1", expect.objectContaining({ entries: [expect.objectContaining({ mapping_id: "mapping-financial", mapping_version: 3 })] })));
    expect(createMapping).not.toHaveBeenCalled();
  });

  it("freezes source-bound mapping controls while an apply request is in flight", async () => {
    const source = { source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64) };
    const descriptor = { table_locator: "transactions", ordered_headers: ["memo"], inferred_types: ["string"], row_count: 1, schema_fingerprint: "d".repeat(64) };
    let resolveMappingSet!: (value: Awaited<ReturnType<typeof api.createMappingSet>>) => void;
    const pendingMappingSet = new Promise<Awaited<ReturnType<typeof api.createMappingSet>>>((resolve) => { resolveMappingSet = resolve; });
    vi.spyOn(api, "schemaDescriptors").mockResolvedValue({ document_id: "document-1", source, descriptors: [descriptor] });
    vi.spyOn(api, "mappings").mockResolvedValue({ document_id: "document-1", source, items: [] });
    vi.spyOn(api, "createMapping").mockImplementation(async (_documentId, body) => ({ ...body, id: "mapping-1", mapping_version: 1, mapping_digest: "f".repeat(64), created_at: "2026-08-23T00:00:00Z" }));
    vi.spyOn(api, "previewMappingSet").mockResolvedValue({ document_id: "document-1", source, candidate_count: 1, reconciliation_counts: { mapped_candidate: 1 }, previews: [{ table_locator: "transactions", rows: [], total_rows: 1, valid_rows: 1, error_rows: 0, blank_rows: 0, truncated: false }] });
    const createSet = vi.spyOn(api, "createMappingSet").mockReturnValue(pendingMappingSet);
    vi.spyOn(api, "applyMappingSet").mockResolvedValue({
      id: "batch-1",
      document_id: "document-1",
      source_intake_id: source.source_intake_id,
      source_file_id: source.source_file_id,
      source_version: source.source_version,
      source_sha256: source.source_sha256,
      normalized_sha256: source.normalized_sha256,
      structure_fingerprint: source.structure_fingerprint,
      mapping_set_id: "set-1",
      mapping_set_version: 1,
      mapping_set_digest: "9".repeat(64),
      lifecycle: "open",
      candidate_count: 1,
      reconciliation_counts: { mapped_candidate: 1 },
      reconciliation_digest: "8".repeat(64),
      version: 1,
      replayed: false,
    });
    const onApplied = vi.fn();

    render(<I18nProvider><MappingWorkspace documentId="document-1" onApplied={onApplied} /></I18nProvider>);
    const destination = await screen.findByLabelText("Destination field for memo");
    fireEvent.click(screen.getByRole("button", { name: "Save definitions and preview" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Create candidate batch" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Create candidate batch" }));
    await waitFor(() => expect(createSet).toHaveBeenCalledTimes(1));

    expect(destination).toBeDisabled();
    expect(screen.getByLabelText("Reviewer")).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Map this structure" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "Back to intake" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("link", { name: "Back to intake" })).toHaveAttribute("tabindex", "-1");

    await act(async () => {
      resolveMappingSet({
        id: "set-1",
        document_id: "document-1",
        source,
        set_digest: "9".repeat(64),
        version: 1,
        created_by: "local-user",
        created_at: "2026-08-23T00:00:00Z",
        entries: [{ table_locator: "transactions", schema_fingerprint: descriptor.schema_fingerprint, mapping_id: "mapping-1", mapping_version: 1, ordinal: 1 }],
      });
      await pendingMappingSet;
    });
    await waitFor(() => expect(onApplied).toHaveBeenCalledWith("batch-1"));
  });
});
