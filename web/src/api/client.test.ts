import { describe, expect, it, vi } from "vitest";

import { api, LocalApiContractError, LocalApiError } from "@/api/client";

describe("local API client", () => {
  it("uses relative URLs and preserves a structured stale-review conflict", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: "stale_extraction", message: "Reload this item" }), { status: 409 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.approve("extract-1", 2, {}, "reviewer")).rejects.toMatchObject({ status: 409, code: "stale_extraction" } satisfies Partial<LocalApiError>);
    expect(fetchMock).toHaveBeenCalledWith("/review/approve", expect.objectContaining({ method: "POST" }));
  });

  it("maps upload limits, unsupported files, validation, and readiness failures", async () => {
    const failures: Array<[number, string]> = [[413, "resource_limit_exceeded"], [415, "unsupported_file"], [422, "invalid_review"], [503, "not_ready"]];
    for (const [status, code] of failures) {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ code, message: code }), { status })));
      await expect(api.ready()).rejects.toMatchObject({ status, code } satisfies Partial<LocalApiError>);
    }
  });

  it("creates an immutable original URL with an exact source version", () => {
    expect(api.originalPath("document-1", 7, "source-1")).toBe("/documents/document-1/original?version=7&source_file_id=source-1");
  });

  it("keeps delayed processing additive to a ready legacy intake response", async () => {
    const readiness = {
      status: "ready",
      intake_ready: true,
      review_ready: true,
      processing_ready: false,
      universal_processing_ready: false,
      processing_reason_codes: ["model_unavailable"],
      registry_digest: "api-registry",
      capabilities_digest: "api-capabilities",
      worker_registry_digest: null,
      worker_capabilities_digest: null,
      worker_capability_lease_age_seconds: null,
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(readiness))));

    await expect(api.ready()).resolves.toEqual(readiness);
  });

  it("keeps intake available when readiness is 503 only because models are delayed", async () => {
    const detail = {
      intake_ready: true,
      review_ready: true,
      processing_ready: false,
      universal_processing_ready: false,
      processing_reason_codes: ["model_unavailable"],
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      code: "not_ready",
      message: "models delayed",
      detail,
    }), { status: 503 })));

    await expect(api.ready()).resolves.toEqual({ status: "not_ready", ...detail });
  });

  it("reads the server capability registry as the format authority", async () => {
    const capabilities = {
      schema: "clerksan.universal-intake-capabilities",
      version: 1,
      process: ["csv", "xlsx"],
      sandbox_verified: true,
      registry_digest: "a".repeat(64),
      capabilities_digest: "b".repeat(64),
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(capabilities)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.capabilities()).resolves.toEqual(capabilities);
    expect(fetchMock).toHaveBeenCalledWith("/capabilities", {});
  });

  it("preserves the unkeyed legacy upload request and response fields", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      document_id: "document-1",
      status: "uploaded",
      duplicate_of: null,
    }), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.upload(new File(["image"], "receipt.png", { type: "image/png" }))).resolves.toEqual({
      document_id: "document-1",
      status: "uploaded",
      duplicate_of: null,
    });
    expect(fetchMock).toHaveBeenCalledWith("/documents", expect.objectContaining({
      method: "POST",
      headers: { Accept: "application/json" },
    }));
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(request.headers).has("Idempotency-Key")).toBe(false);
  });

  it("binds explicit upload intent to its stable idempotency key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      document_id: "document-1",
      status: "uploaded",
      source_file_id: "source-1",
      source_intake_id: "intake-1",
    }), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["date,amount"], "paypay.csv", { type: "text/csv" });

    await api.upload(file, "generic_file", "00000000-0000-4000-8000-000000000001");

    expect(fetchMock).toHaveBeenCalledWith("/documents", expect.objectContaining({ method: "POST" }));
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(request.headers).get("Idempotency-Key")).toBe("00000000-0000-4000-8000-000000000001");
    const body = request.body as FormData;
    expect(body.get("intake_intent")).toBe("generic_file");
    expect(body.get("file")).toMatchObject({ name: "paypay.csv", type: "text/csv" });
  });

  it("polls and retries the exact source intake rather than document status", async () => {
    const detail = {
      intake_id: "intake-1",
      document_id: "document-1",
      source_file_id: "source-1",
      source_version: 2,
      source_sha256: "a".repeat(64),
      upload_idempotency_key: "00000000-0000-4000-8000-000000000001",
      intake_intent: "bill_scan",
      state: "failed",
      reason_code: "processing_failed",
      retryable: true,
      failure_phase: "extract",
      version: 3,
      job_reference: null,
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(detail)))
      .mockResolvedValueOnce(new Response(JSON.stringify({ document_id: "document-1", original_version: 2, status: "queued", job_id: "job-1" }), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.intake("intake-1")).resolves.toEqual(detail);
    await api.retryIntake("intake-1", 3);

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/intakes/intake-1");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/intakes/intake-1/retry");
    const retryRequest = fetchMock.mock.calls[1]?.[1] as RequestInit;
    expect(JSON.parse(String(retryRequest.body))).toEqual({ expected_version: 3, actor: "local-user" });
  });

  it("retains upload idempotency identity from bounded recent-intake rehydration", async () => {
    const detail = {
      intake_id: "intake-1",
      document_id: "document-1",
      source_file_id: "source-1",
      source_version: 1,
      source_sha256: "b".repeat(64),
      upload_idempotency_key: "00000000-0000-4000-8000-000000000009",
      intake_intent: "generic_file",
      state: "processing",
      reason_code: "processing_queued",
      retryable: false,
      failure_phase: null,
      version: 2,
      job_reference: null,
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify([detail])));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.recentIntakes(25)).resolves.toEqual([detail]);
    expect(fetchMock).toHaveBeenCalledWith("/intakes?limit=25", {});
  });

  it("sends exact-source mapping versions from preview through batch creation", async () => {
    const source = {
      source_intake_id: "intake-1", source_file_id: "source-1", source_version: 2,
      source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64),
    };
    const draft = {
      source, idempotency_key: "set-key", created_by: "reviewer", preview_limit: 50,
      entries: [{ table_locator: "table/1", schema_fingerprint: "d".repeat(64), mapping_id: "mapping-1", mapping_version: 3 }],
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        document_id: "document-1",
        source,
        previews: [{
          table_locator: "table/1",
          rows: [
            { row_ordinal: 1, source_locator: "table/1/1", values: { amount: 1 }, errors: [] },
            { row_ordinal: 2, source_locator: "table/1/2", values: { amount: 2 }, errors: [] },
          ],
          total_rows: 2,
          valid_rows: 2,
          error_rows: 0,
          blank_rows: 0,
          truncated: false,
        }],
        reconciliation_counts: { mapped_candidate: 2 },
        candidate_count: 2,
      })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "set-1", document_id: "document-1", source, set_digest: "e".repeat(64), version: 1, created_by: "reviewer", created_at: "2026-08-23T00:00:00Z", entries: [{ ordinal: 0, ...draft.entries[0] }] }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "batch-1", document_id: "document-1", source_intake_id: "intake-1", source_file_id: "source-1", source_version: 2, source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64), mapping_set_id: "set-1", mapping_set_version: 1, mapping_set_digest: "e".repeat(64), lifecycle: "open", candidate_count: 2, reconciliation_counts: {}, reconciliation_digest: "f".repeat(64), version: 1, replayed: false }), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.previewMappingSet("document-1", draft);
    const mappingSet = await api.createMappingSet("document-1", draft);
    await api.applyMappingSet("document-1", mappingSet.id, { source, mapping_set_version: 1, mapping_set_digest: mappingSet.set_digest, expected_mapping_versions: { "mapping-1": 3 }, idempotency_key: "apply-key" });

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/documents/document-1/mapping-sets/preview",
      "/documents/document-1/mapping-sets",
      "/documents/document-1/mapping-sets/set-1/apply",
    ]);
    expect(JSON.parse(String((fetchMock.mock.calls[2]?.[1] as RequestInit).body))).toMatchObject({ expected_mapping_versions: { "mapping-1": 3 }, source });
  });

  it("does not apply a mapping set that differs from the reviewed request", async () => {
    const source = {
      source_intake_id: "intake-1", source_file_id: "source-1", source_version: 2,
      source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64),
    };
    const draft = {
      source, idempotency_key: "set-key", created_by: "reviewer", preview_limit: 50,
      entries: [{ table_locator: "table/1", schema_fingerprint: "d".repeat(64), mapping_id: "mapping-reviewed", mapping_version: 3 }],
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      id: "set-old",
      document_id: "document-1",
      source,
      set_digest: "e".repeat(64),
      version: 1,
      created_by: "reviewer",
      created_at: "2026-08-23T00:00:00Z",
      entries: [{ ordinal: 0, ...draft.entries[0], mapping_id: "mapping-old" }],
    }), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);

    const createThenApply = async (): Promise<void> => {
      const mappingSet = await api.createMappingSet("document-1", draft);
      await api.applyMappingSet("document-1", mappingSet.id, {
        source,
        mapping_set_version: mappingSet.version,
        mapping_set_digest: mappingSet.set_digest,
        expected_mapping_versions: { "mapping-reviewed": 3 },
        idempotency_key: "apply-key",
      });
    };

    await expect(createThenApply()).rejects.toMatchObject({
      contract: "created mapping set",
      code: "invalid_success_response",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("fails closed when an apply response is bound to another mapping authority", async () => {
    const source = {
      source_intake_id: "intake-1", source_file_id: "source-1", source_version: 2,
      source_sha256: "a".repeat(64), normalized_sha256: "b".repeat(64), structure_fingerprint: "c".repeat(64),
    };
    const body = {
      source,
      mapping_set_version: 1,
      mapping_set_digest: "e".repeat(64),
      expected_mapping_versions: { "mapping-1": 3 },
      idempotency_key: "apply-key",
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      id: "batch-1",
      document_id: "document-1",
      source_intake_id: source.source_intake_id,
      source_file_id: source.source_file_id,
      source_version: source.source_version,
      source_sha256: source.source_sha256,
      normalized_sha256: source.normalized_sha256,
      structure_fingerprint: source.structure_fingerprint,
      mapping_set_id: "set-other",
      mapping_set_version: 1,
      mapping_set_digest: body.mapping_set_digest,
      lifecycle: "open",
      candidate_count: 1,
      reconciliation_counts: { mapped_candidate: 1 },
      reconciliation_digest: "f".repeat(64),
      version: 1,
      replayed: false,
    }), { status: 201 })));

    await expect(api.applyMappingSet("document-1", "set-reviewed", body)).rejects.toMatchObject({
      contract: "applied mapping set",
      code: "invalid_success_response",
    });
  });

  it("keeps batch decisions and activation on separate optimistic requests", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ batch_id: "batch-1", previous_batch_version: 4, batch_version: 5, lifecycle: "ready_to_activate", decisions: [] }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ batch_id: "batch-1", document_id: "document-1", source_intake_id: "intake-1", source_file_id: "source-1", source_version: 1, batch_version: 5, lifecycle: "ready_to_activate", total_count: 1, pending_count: 0, included_count: 1, excluded_count: 0, error_count: 0, reconciliation_counts: {}, reconciliation_digest: "a".repeat(64), candidate_count_matches: true, source_is_current: true, requires_accept_exclusions: false, requires_accept_empty: false, ready_for_activation: true, activation_vector_sha256: "b".repeat(64), errors: [] })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ batch_id: "batch-1", document_id: "document-1", batch_version: 6, lifecycle: "active", activation_vector_sha256: "b".repeat(64), included_count: 1, excluded_count: 0, accepted_exclusions: false, accepted_empty: false, verified_by_extraction: { "extract-1": "verified-1" } })));
    vi.stubGlobal("fetch", fetchMock);
    await api.decideReviewBatch("batch-1", { expected_batch_version: 4, actor: "reviewer", decisions: [{ extraction_id: "extract-1", expected_extraction_version: 2, expected_decision_revision: 0, action: "include" }] });
    const preview = await api.activationPreview("batch-1");
    await api.activateReviewBatch("batch-1", { expected_batch_version: preview.batch_version, expected_vector_sha256: preview.activation_vector_sha256, actor: "reviewer", accept_exclusions: false, accept_empty: false });

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/review/batches/batch-1/decisions", "/review/batches/batch-1/activation-preview", "/review/batches/batch-1/activate",
    ]);
    expect(JSON.parse(String((fetchMock.mock.calls[0]?.[1] as RequestInit).body))).toMatchObject({ expected_batch_version: 4, decisions: [{ expected_extraction_version: 2, expected_decision_revision: 0 }] });
    expect(JSON.parse(String((fetchMock.mock.calls[2]?.[1] as RequestInit).body))).toMatchObject({ expected_batch_version: 5, expected_vector_sha256: "b".repeat(64) });
  });

  it("requests one bounded exception-only candidate page", async () => {
    const candidate = {
      extraction_id: "extract-51",
      batch_id: "batch-1",
      candidate_ordinal: 51,
      candidate_key: "c".repeat(64),
      row_fingerprint: null,
      record_kind: "financial",
      financial_subtype: "transaction",
      source_locator: "transactions/51",
      version: 2,
      status: "pending_review",
      payload: { total_amount: 100 },
      field_confidences: {},
      source_spans: {},
      validation_issues: ["amount:check"],
      evidence_group_keys: [],
      latest_decision: null,
      duplicate_evidence: [],
    };
    const page = {
      batch_id: "batch-1",
      batch_version: 3,
      items: [candidate],
      source_duplicate_evidence: [],
      total: 51,
      limit: 25,
      offset: 50,
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(page)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.reviewCandidates("batch-1", 25, 50, true)).resolves.toEqual(page);
    expect(fetchMock).toHaveBeenCalledWith(
      "/review/batches/batch-1/candidates?limit=25&offset=50&exceptions_only=true",
      {},
    );
  });

  it("fails closed with a typed error for malformed authority-driving 2xx responses", async () => {
    const source = {
      source_intake_id: "intake-1",
      source_file_id: "source-1",
      source_version: 1,
      source_sha256: "a".repeat(64),
      normalized_sha256: "b".repeat(64),
      structure_fingerprint: "c".repeat(64),
    };
    const draft = {
      source,
      idempotency_key: "preview-1",
      entries: [],
      created_by: "reviewer",
      preview_limit: 50,
    };
    const calls = [
      () => api.capabilities(),
      () => api.previewMappingSet("document-1", draft),
      () => api.reviewCandidates("batch-1", 50, 0, true),
      () => api.activationPreview("batch-1"),
    ];

    for (const call of calls) {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ unexpected: true }))));
      const result = call();
      await expect(result).rejects.toBeInstanceOf(LocalApiContractError);
      await expect(result).rejects.toMatchObject({ code: "invalid_success_response" });
    }

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("not-json")));
    await expect(api.capabilities()).rejects.toBeInstanceOf(LocalApiContractError);
  });

  it("rejects well-shaped success data when request identity or reconciliation changes", async () => {
    const source = {
      source_intake_id: "intake-1",
      source_file_id: "source-1",
      source_version: 1,
      source_sha256: "a".repeat(64),
      normalized_sha256: "b".repeat(64),
      structure_fingerprint: "c".repeat(64),
    };
    const draft = { source, idempotency_key: "preview-1", entries: [], created_by: "reviewer", preview_limit: 50 };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      document_id: "different-document",
      source,
      previews: [],
      reconciliation_counts: {},
      candidate_count: 0,
    }))));
    await expect(api.previewMappingSet("document-1", draft)).rejects.toMatchObject({
      contract: "mapping preview",
      code: "invalid_success_response",
    });

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      batch_id: "different-batch",
      batch_version: 1,
      items: [],
      source_duplicate_evidence: [],
      total: 0,
      limit: 50,
      offset: 0,
    }))));
    await expect(api.reviewCandidates("batch-1")).rejects.toMatchObject({
      contract: "review candidate page",
      code: "invalid_success_response",
    });

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      batch_id: "batch-1",
      document_id: "document-1",
      source_intake_id: "intake-1",
      source_file_id: "source-1",
      source_version: 1,
      batch_version: 1,
      lifecycle: "ready_to_activate",
      total_count: 2,
      pending_count: 0,
      included_count: 1,
      excluded_count: 0,
      error_count: 0,
      reconciliation_counts: {},
      reconciliation_digest: "d".repeat(64),
      candidate_count_matches: true,
      source_is_current: true,
      requires_accept_exclusions: false,
      requires_accept_empty: false,
      ready_for_activation: true,
      activation_vector_sha256: "e".repeat(64),
      errors: [],
    }))));
    await expect(api.activationPreview("batch-1")).rejects.toMatchObject({
      contract: "activation preview",
      code: "invalid_success_response",
    });
  });

  it("rejects unsupported capability versions and semantically incoherent bounded responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      schema: "clerksan.universal-intake-capabilities",
      version: 2,
      process: ["csv"],
      sandbox_verified: true,
      registry_digest: "a".repeat(64),
      capabilities_digest: "b".repeat(64),
    }))));
    await expect(api.capabilities()).rejects.toMatchObject({
      contract: "capabilities",
      code: "invalid_success_response",
    });

    const source = {
      source_intake_id: "intake-1",
      source_file_id: "source-1",
      source_version: 1,
      source_sha256: "a".repeat(64),
      normalized_sha256: "b".repeat(64),
      structure_fingerprint: "c".repeat(64),
    };
    const draft = {
      source,
      idempotency_key: "preview-1",
      entries: [{ table_locator: "table/1", schema_fingerprint: "d".repeat(64), mapping_id: "mapping-1", mapping_version: 1 }],
      created_by: "reviewer",
      preview_limit: 50,
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      document_id: "document-1",
      source,
      previews: [{
        table_locator: "table/1",
        rows: [],
        total_rows: 1,
        valid_rows: 1,
        error_rows: 0,
        blank_rows: 0,
        truncated: false,
      }],
      reconciliation_counts: { mapped_candidate: 1 },
      candidate_count: 1,
    }))));
    await expect(api.previewMappingSet("document-1", draft)).rejects.toMatchObject({
      contract: "mapping preview",
      code: "invalid_success_response",
    });

    const oversizedItems = Array.from({ length: 51 }, (_, index) => ({
      extraction_id: `extract-${index + 1}`,
      batch_id: "batch-1",
      candidate_ordinal: index + 1,
      candidate_key: (index + 1).toString(16).padStart(64, "0"),
      row_fingerprint: null,
      record_kind: "financial",
      financial_subtype: "transaction",
      source_locator: `transactions/${index + 1}`,
      version: 1,
      status: "pending_review",
      payload: {},
      field_confidences: {},
      source_spans: {},
      validation_issues: [],
      evidence_group_keys: [],
      latest_decision: null,
      duplicate_evidence: [],
    }));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      batch_id: "batch-1",
      batch_version: 1,
      items: oversizedItems,
      source_duplicate_evidence: [],
      total: 51,
      limit: 50,
      offset: 0,
    }))));
    await expect(api.reviewCandidates("batch-1", 50, 0)).rejects.toMatchObject({
      contract: "review candidate page",
      code: "invalid_success_response",
    });
  });

  it("accepts an empty later candidate page after the server cohort shrinks", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      batch_id: "batch-1",
      batch_version: 2,
      items: [],
      source_duplicate_evidence: [],
      total: 0,
      limit: 50,
      offset: 50,
    }))));

    await expect(api.reviewCandidates("batch-1", 50, 50)).resolves.toMatchObject({
      batch_id: "batch-1",
      items: [],
      total: 0,
      offset: 50,
    });
  });

  it("rejects candidate pages whose kind, subtype, or status violates the server contract", async () => {
    const candidate = {
      extraction_id: "extract-1",
      batch_id: "batch-1",
      candidate_ordinal: 1,
      candidate_key: "c".repeat(64),
      row_fingerprint: null,
      record_kind: "financial",
      financial_subtype: "transaction",
      source_locator: "transactions/1",
      version: 1,
      status: "pending_review",
      payload: {},
      field_confidences: {},
      source_spans: {},
      validation_issues: [],
      evidence_group_keys: [],
      latest_decision: null,
      duplicate_evidence: [],
    };
    const malformedCandidates = [
      { ...candidate, financial_subtype: null },
      { ...candidate, record_kind: "generic_document", financial_subtype: "invoice" },
      { ...candidate, status: "arbitrary" },
    ];

    for (const malformedCandidate of malformedCandidates) {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
        batch_id: "batch-1",
        batch_version: 1,
        items: [malformedCandidate],
        source_duplicate_evidence: [],
        total: 1,
        limit: 50,
        offset: 0,
      }))));

      await expect(api.reviewCandidates("batch-1", 50, 0)).rejects.toMatchObject({
        contract: "review candidate page",
        code: "invalid_success_response",
      });
    }
  });

  it("builds exact preview URLs with source version and checksum", () => {
    expect(api.pdfPreviewPagePath("document-1", "source-1", 3, 2, "a".repeat(64))).toBe(`/documents/document-1/sources/source-1/pdf-preview/pages/3?version=2&sha256=${"a".repeat(64)}`);
  });

});
