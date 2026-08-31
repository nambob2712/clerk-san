import type {
  ActivationPreview,
  Answer,
  Bill,
  BillAnalysis,
  BillReminders,
  Capabilities,
  DocumentRecord,
  ErrorPayload,
  ExtractionBatch,
  ExplicitIntakeIntent,
  JobAccepted,
  JsonRecord,
  MappingCreate,
  MappingSet,
  MappingSetApply,
  MappingSetDraft,
  MappingSetPreview,
  Mappings,
  Page,
  PdfPreviewManifest,
  Readiness,
  ReviewBatchActivation,
  ReviewBatchDecisionRequest,
  ReviewBatchDecisionResult,
  ReviewBatchPage,
  ReviewBatchReprocess,
  ReviewCandidatePage,
  ReviewDecision,
  ReviewItem,
  SchemaDescriptors,
  SourceIntakeDetail,
  UploadAccepted,
} from "@/api/contracts";
import {
  parseAppliedMappingSet,
  parseActivationPreview,
  parseCapabilities,
  parseCreatedMappingSet,
  parseMappingSetPreview,
  parseReviewCandidatePage,
} from "@/api/runtime-contracts";

export { LocalApiContractError } from "@/api/runtime-contracts";

const JSON_HEADERS = { Accept: "application/json", "Content-Type": "application/json" };

export class LocalApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail?: JsonRecord;

  constructor(status: number, payload: ErrorPayload) {
    super(payload.message);
    this.name = "LocalApiError";
    this.status = status;
    this.code = payload.code;
    this.detail = payload.detail;
  }

  get isStaleReview(): boolean {
    return this.status === 409 && this.code === "stale_extraction";
  }

  get isStaleSourceIntake(): boolean {
    return this.status === 409 && this.code === "source_intake_stale";
  }

  get isStaleMapping(): boolean {
    return this.status === 409 && this.code.startsWith("stale_");
  }

  get isStaleBatch(): boolean {
    return this.status === 409 && ["stale_review_batch", "stale_activation_preview"].includes(this.code);
  }
}

function ensureRelative(path: string): string {
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new Error("Clerk-san API requests must use a same-origin relative path.");
  }
  return path;
}

function normalizeError(status: number, body: unknown): ErrorPayload {
  if (typeof body === "object" && body !== null) {
    const candidate = body as { code?: unknown; message?: unknown; detail?: unknown };
    if (typeof candidate.code === "string" && typeof candidate.message === "string") {
      return {
        code: candidate.code,
        message: candidate.message,
        detail: isRecord(candidate.detail) ? candidate.detail : undefined,
      };
    }
    if (isRecord(candidate.detail)) {
      const detailCode = candidate.detail.code;
      const detailMessage = candidate.detail.message;
      if (typeof detailCode === "string" && typeof detailMessage === "string") {
        return { code: detailCode, message: detailMessage, detail: candidate.detail };
      }
    }
    if (typeof candidate.detail === "string") {
      return { code: "http_error", message: candidate.detail };
    }
  }
  return { code: "http_error", message: `Local API returned HTTP ${status}.` };
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

type SuccessParser<T> = (body: unknown, endpoint: string) => T;

async function request<T>(path: string, init: RequestInit = {}, parseSuccess?: SuccessParser<T>): Promise<T> {
  const response = await fetch(ensureRelative(path), init);
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text) as unknown;
    } catch {
      if (!response.ok) throw new LocalApiError(response.status, normalizeError(response.status, null));
      if (parseSuccess) return parseSuccess(null, path);
      throw new Error("Local API returned invalid JSON.");
    }
  }
  if (!response.ok) throw new LocalApiError(response.status, normalizeError(response.status, body));
  return parseSuccess ? parseSuccess(body, path) : body as T;
}

async function readiness(): Promise<Readiness> {
  try {
    return await request<Readiness>("/ready");
  } catch (reason) {
    if (
      reason instanceof LocalApiError
      && reason.code === "not_ready"
      && reason.detail?.intake_ready === true
    ) {
      return { status: "not_ready", ...reason.detail } as Readiness;
    }
    throw reason;
  }
}

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : "";
}

export const api = {
  health: (): Promise<{ status: string }> => request("/health"),
  ready: readiness,
  capabilities: (): Promise<Capabilities> => request("/capabilities", {}, parseCapabilities),
  upload: (
    file: File,
    intakeIntent?: ExplicitIntakeIntent,
    idempotencyKey?: string,
  ): Promise<UploadAccepted> => {
    const body = new FormData();
    body.append("file", file, file.name);
    if (intakeIntent) body.append("intake_intent", intakeIntent);
    const headers: Record<string, string> = { Accept: "application/json" };
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    return request("/documents", { method: "POST", body, headers });
  },
  intake: (intakeId: string): Promise<SourceIntakeDetail> => request(`/intakes/${encodeURIComponent(intakeId)}`),
  recentIntakes: (limit = 50): Promise<SourceIntakeDetail[]> => request(`/intakes${query({ limit })}`),
  retryIntake: (intakeId: string, expectedVersion: number, actor = "local-user"): Promise<JobAccepted> =>
    request(`/intakes/${encodeURIComponent(intakeId)}/retry`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ expected_version: expectedVersion, actor }),
    }),
  reprocessIntake: (intakeId: string, expectedVersion: number, actor = "local-user"): Promise<JobAccepted> =>
    request(`/intakes/${encodeURIComponent(intakeId)}/reprocess`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ expected_version: expectedVersion, actor }),
    }),
  status: (documentId: string): Promise<DocumentRecord> => request(`/documents/${documentId}/status`),
  document: (documentId: string): Promise<DocumentRecord> => request(`/documents/${encodeURIComponent(documentId)}`),
  documents: (filters: Record<string, string | number | undefined> = {}): Promise<Page<DocumentRecord>> =>
    request(`/documents${query(filters)}`),
  schemaDescriptors: (documentId: string): Promise<SchemaDescriptors> =>
    request(`/documents/${encodeURIComponent(documentId)}/schema-descriptors`),
  mappings: (documentId: string): Promise<Mappings> =>
    request(`/documents/${encodeURIComponent(documentId)}/mappings`),
  createMapping: (documentId: string, body: MappingCreate): Promise<import("@/api/contracts").SchemaMapping> =>
    request(`/documents/${encodeURIComponent(documentId)}/mappings`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }),
  previewMappingSet: (documentId: string, body: MappingSetDraft): Promise<MappingSetPreview> => {
    const path = `/documents/${encodeURIComponent(documentId)}/mapping-sets/preview`;
    return request(path, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }, (value, endpoint) => parseMappingSetPreview(
      value,
      endpoint,
      documentId,
      body.source,
      body.preview_limit ?? 50,
      body.entries.filter((entry) => entry.mapping_id !== undefined).map((entry) => entry.table_locator),
    ));
  },
  createMappingSet: (documentId: string, body: MappingSetDraft): Promise<MappingSet> => {
    const path = `/documents/${encodeURIComponent(documentId)}/mapping-sets`;
    return request(path, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }, (value, endpoint) => parseCreatedMappingSet(value, endpoint, documentId, body));
  },
  applyMappingSet: (documentId: string, mappingSetId: string, body: MappingSetApply): Promise<ExtractionBatch> => {
    const path = `/documents/${encodeURIComponent(documentId)}/mapping-sets/${encodeURIComponent(mappingSetId)}/apply`;
    return request(path, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }, (value, endpoint) => parseAppliedMappingSet(value, endpoint, documentId, mappingSetId, body));
  },
  pendingReview: (): Promise<ReviewItem[]> => request("/review"),
  reviewBatches: (filters: { limit?: number; offset?: number; lifecycle?: string } = {}): Promise<ReviewBatchPage> =>
    request(`/review/batches${query(filters)}`),
  reviewCandidates: (batchId: string, limit = 50, offset = 0, exceptionsOnly = false): Promise<ReviewCandidatePage> => {
    const path = `/review/batches/${encodeURIComponent(batchId)}/candidates${query({ limit, offset, exceptions_only: exceptionsOnly ? "true" : undefined })}`;
    return request(path, {}, (value, endpoint) => parseReviewCandidatePage(value, endpoint, batchId, limit, offset));
  },
  decideReviewBatch: (batchId: string, body: ReviewBatchDecisionRequest): Promise<ReviewBatchDecisionResult> =>
    request(`/review/batches/${encodeURIComponent(batchId)}/decisions`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }),
  activationPreview: (batchId: string): Promise<ActivationPreview> => {
    const path = `/review/batches/${encodeURIComponent(batchId)}/activation-preview`;
    return request(path, {}, (value, endpoint) => parseActivationPreview(value, endpoint, batchId));
  },
  activateReviewBatch: (
    batchId: string,
    body: {
      expected_batch_version: number;
      expected_vector_sha256: string;
      actor: string;
      accept_exclusions: boolean;
      accept_empty: boolean;
    },
  ): Promise<ReviewBatchActivation> => request(`/review/batches/${encodeURIComponent(batchId)}/activate`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  }),
  rejectAndReprocessBatch: (
    batchId: string,
    expectedBatchVersion: number,
    reason: string,
    actor: string,
  ): Promise<ReviewBatchReprocess> => request(`/review/batches/${encodeURIComponent(batchId)}/reject-and-reprocess`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ expected_batch_version: expectedBatchVersion, reason, actor }),
  }),
  approve: (extractionId: string, expectedVersion: number, corrections: JsonRecord, reviewer: string): Promise<ReviewDecision> =>
    request("/review/approve", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ extraction_id: extractionId, expected_version: expectedVersion, corrections, reviewer }),
    }),
  reject: (extractionId: string, reason: string, reviewer: string): Promise<void> =>
    request("/review/reject", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ extraction_id: extractionId, reason, reviewer }),
    }),
  reprocess: (documentId: string, actor: string): Promise<JobAccepted> =>
    request(`/documents/${documentId}/reprocess${query({ actor })}`, { method: "POST" }),
  retryDerivatives: (documentId: string): Promise<JobAccepted> =>
    request(`/documents/${documentId}/retry-derivatives`, { method: "POST" }),
  originalPath: (documentId: string, sourceVersion: number, sourceFileId: string, sourceSha256?: string): string =>
    `/documents/${encodeURIComponent(documentId)}/original${query({ version: sourceVersion, source_file_id: sourceFileId, sha256: sourceSha256 })}`,
  pdfPreviewManifest: (documentId: string, sourceFileId: string, sourceVersion: number, sourceSha256: string): Promise<PdfPreviewManifest> =>
    request(`/documents/${encodeURIComponent(documentId)}/sources/${encodeURIComponent(sourceFileId)}/pdf-preview${query({ version: sourceVersion, sha256: sourceSha256 })}`),
  pdfPreviewPagePath: (documentId: string, sourceFileId: string, pageNumber: number, sourceVersion: number, sourceSha256: string): string =>
    `/documents/${encodeURIComponent(documentId)}/sources/${encodeURIComponent(sourceFileId)}/pdf-preview/pages/${pageNumber}${query({ version: sourceVersion, sha256: sourceSha256 })}`,
  bills: (): Promise<Bill[]> => request("/bills"),
  billReminders: (daysAhead: number): Promise<BillReminders> => request(`/bills/reminders${query({ days_ahead: daysAhead })}`),
  billAnalysis: (issuerId: string, months: number, anomalyWindow: number): Promise<BillAnalysis> =>
    request(`/bills/${issuerId}/analysis${query({ months, anomaly_window: anomalyWindow })}`),
  markBillPaid: (billId: string, actor: string): Promise<{ status: string; bill_id: string }> =>
    request(`/bills/${billId}/mark-paid${query({ actor })}`, { method: "POST" }),
  ask: (question: string): Promise<Answer> =>
    request("/query", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ question }) }),
};
