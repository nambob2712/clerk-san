import type {
  ActivationPreview,
  Capabilities,
  ExtractionBatch,
  MappingSet,
  MappingSetApply,
  MappingSetDraft,
  MappingSetEntryDraft,
  MappingSetPreview,
  MappingSourceRef,
  ReviewCandidatePage,
} from "@/api/contracts";

type UnknownRecord = Record<string, unknown>;

const SHA256 = /^[0-9a-f]{64}$/u;
const CAPABILITY = /^[a-z0-9][a-z0-9._+-]*$/u;
const RECORD_KINDS = new Set(["financial", "generic_document"]);
const FINANCIAL_SUBTYPES = new Set([
  "transaction",
  "receipt",
  "invoice",
  "bill",
  "recurring_bill",
  "quote",
  "other_financial",
]);
const REVIEW_CANDIDATE_STATUSES = new Set([
  "pending_review",
  "approved",
  "rejected",
  "superseded",
]);
const BATCH_LIFECYCLES = new Set(["open", "ready_to_activate", "active", "superseded", "rejected"]);

export class LocalApiContractError extends Error {
  readonly code = "invalid_success_response";
  readonly contract: string;
  readonly endpoint: string;

  constructor(contract: string, endpoint: string) {
    super(`Local API returned an invalid ${contract} success response for ${endpoint}.`);
    this.name = "LocalApiContractError";
    this.contract = contract;
    this.endpoint = endpoint;
  }
}

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown, maxLength = 4_096): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= maxLength;
}

function isInteger(value: unknown, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): value is number {
  return Number.isSafeInteger(value) && Number(value) >= minimum && Number(value) <= maximum;
}

function isStringArray(value: unknown, maximumItems = 256): value is string[] {
  return Array.isArray(value) && value.length <= maximumItems && value.every((item) => isString(item));
}

function isCountMap(value: unknown, maximumKeys = 512): value is Record<string, number> {
  return isRecord(value)
    && Object.keys(value).length <= maximumKeys
    && Object.entries(value).every(([key, item]) => isString(key) && isInteger(item));
}

function isDigest(value: unknown): value is string {
  return typeof value === "string" && SHA256.test(value);
}

function isOptional<T>(value: unknown, guard: (candidate: unknown) => candidate is T): value is T | null | undefined {
  return value === undefined || value === null || guard(value);
}

function sameSource(left: MappingSourceRef, right: MappingSourceRef): boolean {
  return left.source_intake_id === right.source_intake_id
    && left.source_file_id === right.source_file_id
    && left.source_version === right.source_version
    && left.source_sha256 === right.source_sha256
    && left.normalized_sha256 === right.normalized_sha256
    && left.structure_fingerprint === right.structure_fingerprint;
}

function isMappingSource(value: unknown): value is MappingSourceRef {
  if (!isRecord(value)) return false;
  return isString(value.source_intake_id)
    && isString(value.source_file_id)
    && isInteger(value.source_version, 1)
    && isDigest(value.source_sha256)
    && isDigest(value.normalized_sha256)
    && isDigest(value.structure_fingerprint);
}

function isExpectedMappingSetEntry(
  value: unknown,
  expected: MappingSetEntryDraft,
  expectedOrdinal: number,
): boolean {
  if (!isRecord(value)) return false;
  const mapped = isString(value.mapping_id)
    && isInteger(value.mapping_version, 1)
    && (value.ignore_reason === undefined || value.ignore_reason === null);
  const ignored = (value.mapping_id === undefined || value.mapping_id === null)
    && (value.mapping_version === undefined || value.mapping_version === null)
    && isString(value.ignore_reason, 2_048);
  return (mapped || ignored)
    && value.ordinal === expectedOrdinal
    && value.table_locator === expected.table_locator
    && value.schema_fingerprint === expected.schema_fingerprint
    && (value.mapping_id ?? undefined) === expected.mapping_id
    && (value.mapping_version ?? undefined) === expected.mapping_version
    && (value.ignore_reason ?? undefined) === expected.ignore_reason?.trim();
}

function batchMatchesSource(value: UnknownRecord, source: MappingSourceRef): boolean {
  return value.source_intake_id === source.source_intake_id
    && value.source_file_id === source.source_file_id
    && value.source_version === source.source_version
    && value.source_sha256 === source.source_sha256
    && value.normalized_sha256 === source.normalized_sha256
    && value.structure_fingerprint === source.structure_fingerprint;
}

function isMappingPreviewRow(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return isInteger(value.row_ordinal, 1)
    && isString(value.source_locator)
    && isRecord(value.values)
    && isStringArray(value.errors);
}

function isMappingPreview(value: unknown, expectedLimit: number): boolean {
  if (!isRecord(value) || !Array.isArray(value.rows) || value.rows.length > expectedLimit) return false;
  const rows = value.rows;
  const counts = [value.total_rows, value.valid_rows, value.error_rows, value.blank_rows];
  if (!counts.every((item) => isInteger(item))) return false;
  const totalRows = Number(value.total_rows);
  const rowOrdinals = rows.flatMap((row) => isRecord(row) && isInteger(row.row_ordinal, 1) ? [row.row_ordinal] : []);
  return isString(value.table_locator)
    && rows.every(isMappingPreviewRow)
    && rowOrdinals.length === rows.length
    && new Set(rowOrdinals).size === rows.length
    && Number(value.valid_rows) + Number(value.error_rows) + Number(value.blank_rows) === totalRows
    && rows.length === Math.min(totalRows, expectedLimit)
    && value.truncated === (totalRows > expectedLimit);
}

function isDuplicateEvidence(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return isString(value.id)
    && isString(value.suspected_document_id)
    && isString(value.reason)
    && typeof value.score === "number"
    && Number.isFinite(value.score)
    && value.score >= 0
    && value.score <= 1
    && isRecord(value.evidence)
    && isString(value.scope);
}

function isDecisionRevision(value: unknown, extractionId: string): boolean {
  if (!isRecord(value)) return false;
  return isString(value.id)
    && value.extraction_id === extractionId
    && isInteger(value.decision_revision, 1)
    && (value.action === "include" || value.action === "exclude")
    && isInteger(value.expected_extraction_version, 1)
    && isOptional(value.corrections, isRecord)
    && isOptional(value.corrected_financial_subtype, (item): item is string => typeof item === "string" && FINANCIAL_SUBTYPES.has(item))
    && isOptional(value.exclusion_reason, isString)
    && isString(value.actor)
    && isString(value.created_at);
}

function isReviewCandidate(value: unknown, batchId: string): boolean {
  if (!isRecord(value) || !isString(value.extraction_id)) return false;
  const subtypeMatchesKind = value.record_kind === "financial"
    ? typeof value.financial_subtype === "string" && FINANCIAL_SUBTYPES.has(value.financial_subtype)
    : value.record_kind === "generic_document"
      && (value.financial_subtype === undefined || value.financial_subtype === null);
  return value.batch_id === batchId
    && isInteger(value.candidate_ordinal, 1)
    && isDigest(value.candidate_key)
    && isOptional(value.row_fingerprint, isDigest)
    && typeof value.record_kind === "string"
    && RECORD_KINDS.has(value.record_kind)
    && subtypeMatchesKind
    && isString(value.source_locator)
    && isInteger(value.version, 1)
    && typeof value.status === "string"
    && REVIEW_CANDIDATE_STATUSES.has(value.status)
    && isRecord(value.payload)
    && isRecord(value.field_confidences)
    && isRecord(value.source_spans)
    && isStringArray(value.validation_issues)
    && isStringArray(value.evidence_group_keys)
    && (value.latest_decision === undefined
      || value.latest_decision === null
      || isDecisionRevision(value.latest_decision, value.extraction_id))
    && Array.isArray(value.duplicate_evidence)
    && value.duplicate_evidence.length <= 256
    && value.duplicate_evidence.every(isDuplicateEvidence);
}

function assertContract<T>(
  value: unknown,
  contract: string,
  endpoint: string,
  guard: (candidate: unknown) => boolean,
): T {
  if (!guard(value)) throw new LocalApiContractError(contract, endpoint);
  return value as T;
}

export function parseCapabilities(value: unknown, endpoint: string): Capabilities {
  return assertContract(value, "capabilities", endpoint, (candidate) => {
    if (!isRecord(candidate) || !Array.isArray(candidate.process) || candidate.process.length > 256) return false;
    const process = candidate.process;
    return candidate.schema === "clerksan.universal-intake-capabilities"
      && candidate.version === 1
      && process.every((item) => typeof item === "string" && CAPABILITY.test(item))
      && new Set(process).size === process.length
      && process.every((item, index) => index === 0 || String(process[index - 1]) < String(item))
      && typeof candidate.sandbox_verified === "boolean"
      && (process.length === 0 || candidate.sandbox_verified)
      && isDigest(candidate.registry_digest)
      && isDigest(candidate.capabilities_digest);
  });
}

export function parseMappingSetPreview(
  value: unknown,
  endpoint: string,
  expectedDocumentId: string,
  expectedSource: MappingSourceRef,
  expectedLimit: number,
  expectedTableLocators: readonly string[],
): MappingSetPreview {
  return assertContract(value, "mapping preview", endpoint, (candidate) => {
    if (!isRecord(candidate) || !isMappingSource(candidate.source)) return false;
    if (!Array.isArray(candidate.previews) || candidate.previews.length > 256) return false;
    const previewLocators = candidate.previews.flatMap((preview) =>
      isRecord(preview) && isString(preview.table_locator) ? [preview.table_locator] : [],
    );
    const expectedLocators = [...expectedTableLocators].sort();
    return candidate.document_id === expectedDocumentId
      && sameSource(candidate.source, expectedSource)
      && isInteger(expectedLimit, 1, 50)
      && candidate.previews.every((preview) => isMappingPreview(preview, expectedLimit))
      && previewLocators.length === candidate.previews.length
      && new Set(previewLocators).size === previewLocators.length
      && [...previewLocators].sort().every((locator, index) => locator === expectedLocators[index])
      && previewLocators.length === expectedLocators.length
      && isCountMap(candidate.reconciliation_counts)
      && isInteger(candidate.candidate_count);
  });
}

export function parseCreatedMappingSet(
  value: unknown,
  endpoint: string,
  expectedDocumentId: string,
  expectedDraft: MappingSetDraft,
): MappingSet {
  return assertContract(value, "created mapping set", endpoint, (candidate) => {
    if (!isRecord(candidate) || !isMappingSource(candidate.source) || !Array.isArray(candidate.entries)) return false;
    return candidate.document_id === expectedDocumentId
      && sameSource(candidate.source, expectedDraft.source)
      && isString(candidate.id)
      && isDigest(candidate.set_digest)
      && isInteger(candidate.version, 1)
      && candidate.created_by === expectedDraft.created_by.trim()
      && isString(candidate.created_at)
      && candidate.entries.length === expectedDraft.entries.length
      && candidate.entries.every((entry, index) =>
        isExpectedMappingSetEntry(entry, expectedDraft.entries[index] as MappingSetEntryDraft, index),
      );
  });
}

export function parseAppliedMappingSet(
  value: unknown,
  endpoint: string,
  expectedDocumentId: string,
  expectedMappingSetId: string,
  expectedApply: MappingSetApply,
): ExtractionBatch {
  return assertContract(value, "applied mapping set", endpoint, (candidate) => {
    if (!isRecord(candidate)) return false;
    return candidate.document_id === expectedDocumentId
      && batchMatchesSource(candidate, expectedApply.source)
      && candidate.mapping_set_id === expectedMappingSetId
      && candidate.mapping_set_version === expectedApply.mapping_set_version
      && candidate.mapping_set_digest === expectedApply.mapping_set_digest
      && isString(candidate.id)
      && typeof candidate.lifecycle === "string"
      && BATCH_LIFECYCLES.has(candidate.lifecycle)
      && isInteger(candidate.candidate_count)
      && isCountMap(candidate.reconciliation_counts)
      && isDigest(candidate.reconciliation_digest)
      && isInteger(candidate.version, 1)
      && typeof candidate.replayed === "boolean";
  });
}

export function parseReviewCandidatePage(
  value: unknown,
  endpoint: string,
  expectedBatchId: string,
  expectedLimit: number,
  expectedOffset: number,
): ReviewCandidatePage {
  return assertContract(value, "review candidate page", endpoint, (candidate) => {
    if (!isRecord(candidate) || !Array.isArray(candidate.items) || candidate.items.length > expectedLimit) return false;
    return candidate.batch_id === expectedBatchId
      && isInteger(candidate.batch_version, 1)
      && candidate.items.every((item) => isReviewCandidate(item, expectedBatchId))
      && Array.isArray(candidate.source_duplicate_evidence)
      && candidate.source_duplicate_evidence.length <= 1_000
      && candidate.source_duplicate_evidence.every(isDuplicateEvidence)
      && isInteger(candidate.total)
      && candidate.total >= candidate.items.length
      && (candidate.items.length === 0 || expectedOffset + candidate.items.length <= candidate.total)
      && candidate.limit === expectedLimit
      && candidate.offset === expectedOffset;
  });
}

export function parseActivationPreview(
  value: unknown,
  endpoint: string,
  expectedBatchId: string,
): ActivationPreview {
  return assertContract(value, "activation preview", endpoint, (candidate) => {
    if (!isRecord(candidate) || !Array.isArray(candidate.errors) || candidate.errors.length > 256) return false;
    const counts = [candidate.total_count, candidate.pending_count, candidate.included_count, candidate.excluded_count, candidate.error_count];
    const countsAreValid = counts.every((item) => isInteger(item));
    if (!countsAreValid) return false;
    const total = Number(candidate.total_count);
    const pending = Number(candidate.pending_count);
    const included = Number(candidate.included_count);
    const excluded = Number(candidate.excluded_count);
    return candidate.batch_id === expectedBatchId
      && isString(candidate.document_id)
      && isString(candidate.source_intake_id)
      && isString(candidate.source_file_id)
      && isInteger(candidate.source_version, 1)
      && isInteger(candidate.batch_version, 1)
      && typeof candidate.lifecycle === "string"
      && BATCH_LIFECYCLES.has(candidate.lifecycle)
      && pending + included + excluded === total
      && candidate.error_count === candidate.errors.length
      && isCountMap(candidate.reconciliation_counts)
      && isDigest(candidate.reconciliation_digest)
      && typeof candidate.candidate_count_matches === "boolean"
      && typeof candidate.source_is_current === "boolean"
      && candidate.requires_accept_exclusions === (excluded > 0)
      && candidate.requires_accept_empty === (total === 0)
      && typeof candidate.ready_for_activation === "boolean"
      && (!candidate.ready_for_activation
        || (candidate.errors.length === 0
          && pending === 0
          && candidate.candidate_count_matches
          && candidate.source_is_current))
      && isDigest(candidate.activation_vector_sha256)
      && candidate.errors.every(isRecord);
  });
}
