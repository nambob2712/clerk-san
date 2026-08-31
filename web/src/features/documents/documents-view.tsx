import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { IconFilter, IconRefresh } from "@tabler/icons-react";

import type {
  BatchLifecycle,
  DocumentRecord,
  ReviewBatchSummary,
  SourceIntakeDetail,
  SourceIntakeState,
} from "@/api/contracts";
import { api } from "@/api/client";
import { Button, EmptyState, LoadingPanel, Notice, PageHeading } from "@/components/ui";
import { formatDate, formatMoney, isRecord } from "@/lib/format";
import { useI18n, type TranslationKey } from "@/lib/i18n";

type Translate = ReturnType<typeof useI18n>["t"];
type SupplementLoadState =
  | { status: "loading" }
  | { status: "ready"; complete: boolean }
  | { status: "error"; detail: string };

const DOCUMENT_STATUS_KEYS: Readonly<Record<string, TranslationKey>> = {
  uploaded: "documents.status.uploaded",
  normalized: "documents.status.normalized",
  extracted: "documents.status.extracted",
  in_review: "documents.status.in_review",
  verified: "documents.status.verified",
  needs_reprocess: "documents.status.needs_reprocess",
  failed: "documents.status.failed",
};

const INTAKE_STATE_KEYS: Readonly<Record<SourceIntakeState, TranslationKey>> = {
  queued: "documents.intake_state.queued",
  processing: "documents.intake_state.processing",
  processed: "documents.intake_state.processed",
  needs_mapping: "documents.intake_state.needs_mapping",
  stored_unprocessed: "documents.intake_state.stored_unprocessed",
  failed: "documents.intake_state.failed",
};

const BATCH_LIFECYCLE_KEYS: Readonly<Record<BatchLifecycle, TranslationKey>> = {
  open: "documents.batch_state.open",
  ready_to_activate: "documents.batch_state.ready_to_activate",
  active: "documents.batch_state.active",
  superseded: "documents.batch_state.superseded",
  rejected: "documents.batch_state.rejected",
};

function translatedState(value: string, keys: Readonly<Record<string, TranslationKey>>, t: Translate): string {
  const key = keys[value];
  return key ? t(key) : t("value.unknown");
}

function failureDetail(reason: unknown): string {
  return reason instanceof Error ? reason.message : "";
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/u.test(value);
}

function originalForVerifiedRecord(document: DocumentRecord, verified: Record<string, unknown>) {
  const sourceFileId = verified.source_file_id;
  const sourceVersion = verified.source_version;
  if (typeof sourceFileId !== "string" || typeof sourceVersion !== "number") return null;
  return document.files.find((file) => file.kind === "original" && file.id === sourceFileId && file.version === sourceVersion) ?? null;
}

export function DocumentsView(): React.ReactElement {
  const { t } = useI18n();
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("verified");
  const [appliedFilters, setAppliedFilters] = useState({ query: "", status: "verified" });
  const [loading, setLoading] = useState(true);
  const [documentFailure, setDocumentFailure] = useState<{ detail: string } | null>(null);
  const [intakes, setIntakes] = useState<SourceIntakeDetail[]>([]);
  const [intakeLoad, setIntakeLoad] = useState<SupplementLoadState>({ status: "loading" });
  const [batches, setBatches] = useState<ReviewBatchSummary[]>([]);
  const [batchLoad, setBatchLoad] = useState<SupplementLoadState>({ status: "loading" });
  const documentRequest = useRef(0);
  const intakeRequest = useRef(0);
  const batchRequest = useRef(0);

  const loadIntakes = useCallback(async (): Promise<void> => {
    const requestId = ++intakeRequest.current;
    setIntakes([]);
    setIntakeLoad({ status: "loading" });
    try {
      const next = await api.recentIntakes(100);
      if (intakeRequest.current !== requestId) return;
      setIntakes(next);
      setIntakeLoad({ status: "ready", complete: next.length < 100 });
    } catch (reason) {
      if (intakeRequest.current !== requestId) return;
      setIntakes([]);
      setIntakeLoad({ status: "error", detail: failureDetail(reason) });
    }
  }, []);

  const loadBatches = useCallback(async (): Promise<void> => {
    const requestId = ++batchRequest.current;
    setBatches([]);
    setBatchLoad({ status: "loading" });
    try {
      const next = await api.reviewBatches({ limit: 100 });
      if (batchRequest.current !== requestId) return;
      setBatches(next.items);
      setBatchLoad({ status: "ready", complete: next.items.length < 100 && next.total <= next.items.length });
    } catch (reason) {
      if (batchRequest.current !== requestId) return;
      setBatches([]);
      setBatchLoad({ status: "error", detail: failureDetail(reason) });
    }
  }, []);

  const load = useCallback(async (): Promise<void> => {
    const requestId = ++documentRequest.current;
    setLoading(true);
    setDocumentFailure(null);
    setDocuments([]);
    const supplements = Promise.all([loadIntakes(), loadBatches()]);
    try {
      const page = await api.documents({
        status: appliedFilters.status || undefined,
        counterparty: appliedFilters.query || undefined,
        limit: 100,
      });
      if (documentRequest.current === requestId) setDocuments(page.items);
    } catch (reason) {
      if (documentRequest.current === requestId) setDocumentFailure({ detail: failureDetail(reason) });
    } finally {
      if (documentRequest.current === requestId) setLoading(false);
    }
    await supplements;
  }, [appliedFilters, loadBatches, loadIntakes]);

  useEffect(() => {
    void load();
    return () => {
      documentRequest.current += 1;
      intakeRequest.current += 1;
      batchRequest.current += 1;
    };
  }, [load]);

  const rows = useMemo(() => documents.map((document) => {
    const verified = document.verified && isRecord(document.verified) ? document.verified : {};
    const latestIntake = intakes.find((intake) => intake.document_id === document.id);
    const documentBatches = batches.filter((batch) => batch.document_id === document.id);
    const activeBatch = documentBatches.find((batch) => batch.lifecycle === "active");
    const latestBatch = activeBatch ?? (batchLoad.status === "ready" && batchLoad.complete ? documentBatches[0] : undefined);
    return { document, verified, latestIntake, latestBatch };
  }), [batchLoad, batches, documents, intakes]);

  return <div className="page-stack">
    <PageHeading title={t("page.documents.title")} copy={t("page.documents.copy")} action={<Button className="button-secondary" onClick={() => void load()}><IconRefresh size={18} />{t("action.reload")}</Button>} />
    {documentFailure ? <Notice tone="error">{documentFailure.detail || t("unavailable.title")}</Notice> : null}
    {intakeLoad.status === "error" ? <Notice tone="warning"><span>{t("documents.intakes_unavailable", { detail: intakeLoad.detail || t("value.unknown") })}</span><Button className="button-secondary" onClick={() => void loadIntakes()}>{t("documents.retry_intakes")}</Button></Notice> : null}
    {batchLoad.status === "error" ? <Notice tone="warning"><span>{t("documents.batches_unavailable", { detail: batchLoad.detail || t("value.unknown") })}</span><Button className="button-secondary" onClick={() => void loadBatches()}>{t("documents.retry_batches")}</Button></Notice> : null}
    <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); setAppliedFilters({ query, status }); }}><label><span>{t("history.counterparty")}</span><input value={query} onChange={(event) => setQuery(event.target.value)} /></label><label><span>{t("column.status")}</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="verified">{t("documents.verified")}</option><option value="in_review">{t("documents.pending_review")}</option><option value="">{t("documents.all_states")}</option></select></label><Button className="button-primary" type="submit"><IconFilter size={18} />{t("action.apply_filters")}</Button></form>
    {loading ? <LoadingPanel label={t("loading.document_history")} /> : null}
    {!loading && rows.length === 0 ? <EmptyState title={t("history.no_results")} copy={t("documents.adjust_filters")} /> : null}
    {rows.length ? <section className="data-table-wrap"><table><thead><tr><th>{t("history.date")}</th><th>{t("history.counterparty")}</th><th>{t("history.amount")}</th><th>{t("history.expense_type")}</th><th>{t("history.document")}</th><th>{t("documents.latest_intake")}</th><th>{t("documents.authority")}</th><th>{t("column.status")}</th></tr></thead><tbody>{rows.map(({ document, verified, latestIntake, latestBatch }) => {
      const original = originalForVerifiedRecord(document, verified);
      const sourceLabel = original?.source_filename && typeof original.version === "number" ? `${original.source_filename} · v${original.version}` : document.source_filename;
      const sourceSha256 = isSha256(original?.sha256) ? original.sha256 : null;
      const intakeCell = intakeLoad.status === "error"
        ? t("documents.intake_unknown")
        : intakeLoad.status === "loading"
          ? t("documents.supplement_loading")
          : latestIntake
            ? <><span className="status-chip">{translatedState(latestIntake.state, INTAKE_STATE_KEYS, t)}</span><small className="candidate-meta">v{latestIntake.source_version} · {latestIntake.intake_id.slice(0, 8)}</small></>
            : intakeLoad.complete
              ? t("documents.no_intake")
              : t("documents.intake_not_in_window");
      const batchCell = batchLoad.status === "error"
        ? t("documents.batch_unknown")
        : batchLoad.status === "loading"
          ? t("documents.supplement_loading")
          : latestBatch
            ? <><span className="status-chip">{translatedState(latestBatch.lifecycle, BATCH_LIFECYCLE_KEYS, t)}</span><small className="candidate-meta">{t("documents.batch_counts", { candidates: latestBatch.candidate_count, included: latestBatch.included_count })}</small><small className="candidate-meta">{t("documents.batch_identity", { version: latestBatch.source_version, batch: latestBatch.id.slice(0, 8) })}</small></>
            : batchLoad.complete
              ? t("documents.no_batch")
              : t("documents.batch_not_in_window");
      return <tr key={document.id}><td>{formatDate(typeof verified.transaction_date === "string" ? verified.transaction_date : document.created_at)}</td><td>{typeof verified.counterparty === "string" ? verified.counterparty : t("field.not_extracted")}</td><td>{formatMoney(verified.total_amount, typeof verified.currency === "string" ? verified.currency : "JPY")}</td><td>{typeof verified.expense_kind === "string" ? verified.expense_kind : t("value.unspecified")}</td><td>{original?.id && typeof original.version === "number" && sourceSha256 ? <a href={api.originalPath(document.id, original.version, original.id, sourceSha256)} target="_blank" rel="noreferrer">{sourceLabel}</a> : original ? <>{sourceLabel}<small className="candidate-meta">{t("documents.original_integrity_unavailable")}</small></> : document.source_filename}</td><td>{intakeCell}</td><td>{batchCell}</td><td><span className="status-chip">{translatedState(document.status, DOCUMENT_STATUS_KEYS, t)}</span></td></tr>;
    })}</tbody></table></section> : null}
  </div>;
}

export default DocumentsView;
