export type JsonValue = string | number | boolean | null | JsonRecord | JsonValue[];
export type JsonRecord = { [key: string]: JsonValue | undefined };

export interface ErrorPayload {
  code: string;
  message: string;
  detail?: JsonRecord;
}

export interface Readiness {
  status: string;
  demo_mode?: boolean;
  intake_ready?: boolean;
  review_ready?: boolean;
  processing_ready?: boolean;
  universal_processing_ready?: boolean;
  processing_reason_codes?: string[];
  registry_digest?: string;
  capabilities_digest?: string;
  worker_registry_digest?: string | null;
  worker_capabilities_digest?: string | null;
  worker_capability_lease_age_seconds?: number | null;
}

export interface Capabilities {
  schema: string;
  version: 1;
  process: string[];
  sandbox_verified: boolean;
  registry_digest: string;
  capabilities_digest: string;
}

export interface UploadAccepted {
  document_id: string;
  status: "uploaded";
  duplicate_of?: string | null;
  source_file_id?: string | null;
  source_intake_id?: string | null;
  job_id?: string | null;
  reason_code?: string | null;
  retryable?: boolean | null;
}

export type IntakeIntent = "legacy_unspecified" | "generic_file" | "bill_scan";
export type ExplicitIntakeIntent = Exclude<IntakeIntent, "legacy_unspecified">;
export type SourceIntakeState =
  | "queued"
  | "processing"
  | "processed"
  | "needs_mapping"
  | "stored_unprocessed"
  | "failed";

export interface IntakeJobReference {
  job_id: string;
  job_type: string;
  status: string;
}

export interface SourceIntakeDetail {
  intake_id: string;
  document_id: string;
  source_file_id: string;
  source_version: number;
  source_sha256: string;
  upload_idempotency_key?: string | null;
  intake_intent: IntakeIntent;
  detected_format?: string | null;
  state: SourceIntakeState;
  reason_code?: string | null;
  retryable: boolean;
  failure_phase?: string | null;
  version: number;
  job_reference?: IntakeJobReference | null;
}

export interface DocumentFile extends JsonRecord {
  id?: string;
  kind?: string;
  version?: number;
  source_filename?: string;
  mime?: string;
  sha256?: string;
}

export interface DocumentRecord extends JsonRecord {
  id: string;
  doc_class: string;
  status: string;
  source_filename: string;
  created_at: string;
  updated_at?: string | null;
  files: DocumentFile[];
  extracted?: JsonRecord | null;
  verified?: JsonRecord | null;
  processing_error?: string | null;
  audit_history?: JsonRecord[];
}

export interface Page<T> {
  items: T[];
  limit: number;
  offset: number;
}

export interface FieldEnvelope extends JsonRecord {
  value?: JsonValue;
  confidence?: number;
  source_span?: string;
}

export interface DuplicateCandidate extends JsonRecord {
  document_id: string;
  reason: string;
  score: number;
  evidence?: JsonValue;
}

export interface ReviewItem extends JsonRecord {
  document_id: string;
  extraction_id: string;
  version: number;
  source_file_id: string;
  source_version: number;
  doc_class: string;
  flagged_fields: string[];
  suggested: JsonRecord;
  source_spans: JsonRecord;
  suspected_duplicate_of: string[];
  duplicate_candidates: DuplicateCandidate[];
  batch_id?: string | null;
  batch_version?: number | null;
  batch_candidate_count?: number | null;
  record_kind?: RecordKind | null;
  financial_subtype?: FinancialSubtype | null;
}

export type RecordKind = "financial" | "generic_document";
export type FinancialSubtype =
  | "transaction"
  | "receipt"
  | "invoice"
  | "bill"
  | "recurring_bill"
  | "quote"
  | "other_financial";
export type CandidateDecisionAction = "include" | "exclude";
export type BatchLifecycle = "open" | "ready_to_activate" | "active" | "superseded" | "rejected";

export interface MappingSourceRef {
  source_intake_id: string;
  source_file_id: string;
  source_version: number;
  source_sha256: string;
  normalized_sha256: string;
  structure_fingerprint: string;
}

export type FieldParser = "raw" | "date" | "decimal" | "currency";
export type DateStyle = "iso" | "ymd_slash" | "dmy_slash" | "mdy_slash" | "japanese";
export type DecimalStyle = "dot" | "comma";
export type SignRule = "preserve" | "negate" | "absolute";

export interface MappingFieldRule {
  target_field: string;
  source_columns: string[];
  literal?: string | null;
  separator?: string;
  trim?: boolean;
  null_markers?: string[];
  value_map?: [string, string][];
  parser?: FieldParser;
  date_style?: DateStyle | null;
  decimal_style?: DecimalStyle | null;
  sign_rule?: SignRule;
  currency_aliases?: [string, string][];
}

export interface SchemaDescriptor {
  table_locator: string;
  ordered_headers: string[];
  inferred_types: string[];
  row_count: number;
  schema_fingerprint: string;
}

export interface SchemaDescriptors {
  document_id: string;
  source: MappingSourceRef;
  descriptors: SchemaDescriptor[];
}

export interface MappingCreate {
  source: MappingSourceRef;
  idempotency_key: string;
  table_locator: string;
  schema_fingerprint: string;
  record_kind: RecordKind;
  financial_subtype?: FinancialSubtype | null;
  field_rules: MappingFieldRule[];
  required_fields: string[];
  mapping_version?: number;
  created_by: string;
}

export interface SchemaMapping extends MappingCreate {
  id: string;
  mapping_version: number;
  mapping_digest: string;
  created_at: string;
}

export interface Mappings {
  document_id: string;
  source: MappingSourceRef;
  items: SchemaMapping[];
}

export interface MappingSetEntryDraft {
  table_locator: string;
  schema_fingerprint: string;
  mapping_id?: string;
  mapping_version?: number;
  ignore_reason?: string;
}

export interface MappingSetDraft {
  source: MappingSourceRef;
  idempotency_key: string;
  entries: MappingSetEntryDraft[];
  created_by: string;
  preview_limit?: number;
}

export interface MappingPreviewRow {
  row_ordinal: number;
  source_locator: string;
  values: JsonRecord;
  errors: string[];
}

export interface MappingPreview {
  table_locator: string;
  rows: MappingPreviewRow[];
  total_rows: number;
  valid_rows: number;
  error_rows: number;
  blank_rows: number;
  truncated: boolean;
}

export interface MappingSetPreview {
  document_id: string;
  source: MappingSourceRef;
  previews: MappingPreview[];
  reconciliation_counts: Record<string, number>;
  candidate_count: number;
}

export interface MappingSet {
  id: string;
  document_id: string;
  source: MappingSourceRef;
  set_digest: string;
  version: number;
  created_by: string;
  created_at: string;
  entries: Array<MappingSetEntryDraft & { ordinal: number }>;
}

export interface MappingSetApply {
  source: MappingSourceRef;
  mapping_set_version: number;
  mapping_set_digest: string;
  expected_mapping_versions: Record<string, number>;
  idempotency_key: string;
}

export interface ExtractionBatch {
  id: string;
  document_id: string;
  source_intake_id: string;
  source_file_id: string;
  source_version: number;
  source_sha256: string;
  normalized_sha256: string;
  structure_fingerprint: string;
  mapping_set_id?: string | null;
  mapping_set_version?: number | null;
  mapping_set_digest?: string | null;
  lifecycle: BatchLifecycle;
  candidate_count: number;
  reconciliation_counts: Record<string, number>;
  reconciliation_digest: string;
  version: number;
  replayed: boolean;
}

export interface ReviewBatchSummary {
  id: string;
  document_id: string;
  source_intake_id: string;
  source_file_id: string;
  source_version: number;
  lifecycle: BatchLifecycle;
  version: number;
  candidate_count: number;
  pending_count: number;
  included_count: number;
  excluded_count: number;
  error_count: number;
  exception_count: number;
  reconciliation_counts: Record<string, number>;
  reconciliation_digest: string;
  created_at: string;
  updated_at: string;
}

export interface ReviewBatchPage {
  items: ReviewBatchSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface ReviewDecisionRevision {
  id: string;
  extraction_id: string;
  decision_revision: number;
  action: CandidateDecisionAction;
  expected_extraction_version: number;
  corrections?: JsonRecord | null;
  corrected_financial_subtype?: FinancialSubtype | null;
  exclusion_reason?: string | null;
  actor: string;
  created_at: string;
}

export interface ReviewDuplicateEvidence {
  id: string;
  suspected_document_id: string;
  reason: string;
  score: number;
  evidence: JsonRecord;
  scope: string;
}

export interface ReviewCandidate {
  extraction_id: string;
  batch_id: string;
  candidate_ordinal: number;
  candidate_key: string;
  row_fingerprint?: string | null;
  record_kind: RecordKind;
  financial_subtype?: FinancialSubtype | null;
  source_locator: string;
  version: number;
  status: string;
  payload: JsonRecord;
  field_confidences: JsonRecord;
  source_spans: JsonRecord;
  validation_issues: string[];
  evidence_group_keys: string[];
  latest_decision?: ReviewDecisionRevision | null;
  duplicate_evidence: ReviewDuplicateEvidence[];
}

export interface ReviewCandidatePage {
  batch_id: string;
  batch_version: number;
  items: ReviewCandidate[];
  source_duplicate_evidence: ReviewDuplicateEvidence[];
  total: number;
  limit: number;
  offset: number;
}

export interface ReviewCandidateDecision {
  extraction_id: string;
  expected_extraction_version: number;
  expected_decision_revision: number;
  action: CandidateDecisionAction;
  corrections?: JsonRecord | null;
  corrected_financial_subtype?: FinancialSubtype | null;
  exclusion_reason?: string | null;
}

export interface ReviewBatchDecisionRequest {
  expected_batch_version: number;
  decisions: ReviewCandidateDecision[];
  actor: string;
}

export interface ReviewBatchDecisionResult {
  batch_id: string;
  previous_batch_version: number;
  batch_version: number;
  lifecycle: BatchLifecycle;
  decisions: ReviewDecisionRevision[];
}

export interface ActivationPreview {
  batch_id: string;
  document_id: string;
  source_intake_id: string;
  source_file_id: string;
  source_version: number;
  batch_version: number;
  lifecycle: BatchLifecycle;
  total_count: number;
  pending_count: number;
  included_count: number;
  excluded_count: number;
  error_count: number;
  reconciliation_counts: Record<string, number>;
  reconciliation_digest: string;
  candidate_count_matches: boolean;
  source_is_current: boolean;
  requires_accept_exclusions: boolean;
  requires_accept_empty: boolean;
  ready_for_activation: boolean;
  activation_vector_sha256: string;
  errors: JsonRecord[];
}

export interface ReviewBatchActivation {
  batch_id: string;
  document_id: string;
  batch_version: number;
  lifecycle: BatchLifecycle;
  activation_vector_sha256: string;
  included_count: number;
  excluded_count: number;
  accepted_exclusions: boolean;
  accepted_empty: boolean;
  verified_by_extraction: Record<string, string>;
}

export interface ReviewBatchReprocess {
  batch_id: string;
  document_id: string;
  source_intake_id: string;
  source_file_id: string;
  source_version: number;
  batch_version: number;
  lifecycle: BatchLifecycle;
  status: "queued" | "already_queued";
  job_id?: string | null;
}

export interface ExactSourcePreview {
  document_id: string;
  source_intake_id?: string;
  source_file_id: string;
  source_version: number;
  source_sha256: string;
  filename: string;
  mime: string;
  created_at?: string;
}

export interface PdfPreviewPage {
  page_number: number;
  artifact_id: string;
  sha256: string;
  mime: "image/png";
  width: number;
  height: number;
  byte_size: number;
}

export interface PdfPreviewManifest {
  schema_version: number;
  document_id: string;
  source_file_id: string;
  source_version: number;
  source_sha256: string;
  page_count: number;
  status: "ready" | "unavailable";
  pages: PdfPreviewPage[];
  manifest_sha256: string;
  unavailable_reason?: string | null;
}

export interface ReviewDecision {
  verified_id: string;
  document_id?: string;
}

export interface JobAccepted extends JsonRecord {
  document_id: string;
  original_version: number;
  status: string;
  job_id?: string | null;
  job_ids?: string[];
}

export interface Citation extends JsonRecord {
  document_id: string;
  heading_path: string;
  snippet: string;
}

export interface Answer extends JsonRecord {
  text: string;
  mode: string;
  citations: Citation[];
  sql_result?: JsonRecord | null;
}

export interface Bill extends JsonRecord {
  id: string;
  issuer_id: string;
  issuer: string;
  issuer_kind: string;
  billing_period: string;
  amount: number;
  due_date?: string | null;
  payment_status: string;
  consumption_value?: number | null;
  consumption_unit?: string | null;
}

export interface BillReminder extends JsonRecord {
  id?: string;
  issuer?: string;
  due_date?: string;
  days_until_due?: number;
}

export interface BillReminders {
  upcoming: BillReminder[];
  overdue: BillReminder[];
}

export interface BillAnalysis extends JsonRecord {
  issuer_id: string;
  comparisons: JsonRecord[];
  anomalies: JsonRecord[];
}
