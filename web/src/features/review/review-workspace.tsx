import { useCallback, useEffect, useMemo, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { IconAlertTriangle, IconChevronRight, IconCopyCheck, IconReload, IconSend, IconX } from "@tabler/icons-react";

import type { JsonRecord, JsonValue, ReviewItem } from "@/api/contracts";
import { api, LocalApiError } from "@/api/client";
import OriginalPreview from "@/components/original-preview";
import { Button, EmptyState, LoadingPanel, Notice, PageHeading } from "@/components/ui";
import GroupedReviewWorkspace from "@/features/review/grouped-review-workspace";
import {
  clearLegacyReprocessRecovery,
  persistLegacyReprocessRecovery,
  readLegacyReprocessRecovery,
  type LegacyReprocessRecovery,
} from "@/features/review/legacy-reprocess-recovery";
import { asText, editValue, fieldConfidence, fieldSource, fieldValue, parseEdit } from "@/lib/format";
import { useI18n, type TranslationKey } from "@/lib/i18n";

type Draft = Record<string, string>;

type ReprocessRecoveryReadiness = "checking" | "ready" | "unavailable";

interface ReprocessRecovery extends LegacyReprocessRecovery {
  message?: string;
  readiness: ReprocessRecoveryReadiness;
}

type RecoveryDisposition = "required" | "obsolete" | "unknown";

const RECOVERY_REQUIRED_STATUSES = new Set(["needs_reprocess", "failed"]);
const RECOVERY_OBSOLETE_STATUSES = new Set(["uploaded", "normalized", "extracted", "in_review", "verified"]);

const EDITABLE_CORRECTION_FIELDS = new Set([
  "transaction_date",
  "total_amount",
  "counterparty",
  "currency",
  "category",
  "expense_category",
  "expense_kind",
  "due_date",
  "registration_number",
  "tax_8_amount",
  "tax_10_amount",
]);

const RECURRING_BILL_CORRECTION_FIELDS = new Set([
  "issuer_name",
  "issuer_kind",
  "billing_period",
  "due_date",
  "consumption_value",
  "consumption_unit",
]);

export function isEditableCorrectionField(key: string, documentClass: string): boolean {
  return EDITABLE_CORRECTION_FIELDS.has(key) || (documentClass === "recurring_bill" && RECURRING_BILL_CORRECTION_FIELDS.has(key));
}

function fieldLabel(key: string, t: (key: TranslationKey) => string): string {
  const translated = t(`field.${key}` as TranslationKey);
  return translated === `field.${key}` ? key.replaceAll("_", " ").replace(/\b\w/gu, (letter) => letter.toUpperCase()) : translated;
}

function reviewTitle(item: ReviewItem): string {
  const counterparty = fieldValue(item.suggested.counterparty);
  const amount = fieldValue(item.suggested.total_amount);
  return `${typeof counterparty === "string" ? counterparty : item.doc_class} · ${typeof amount === "number" || typeof amount === "string" ? amount : item.document_id.slice(0, 8)}`;
}

function needsReview(key: string, flags: string[]): boolean {
  return flags.some((flag) => flag === key || flag.startsWith(`${key}.`));
}

function recoveryDisposition(document: unknown, expectedDocumentId: string): RecoveryDisposition {
  if (typeof document !== "object" || document === null) return "unknown";
  const candidate = document as { id?: unknown; status?: unknown };
  if (candidate.id !== expectedDocumentId || typeof candidate.status !== "string") return "unknown";
  if (RECOVERY_REQUIRED_STATUSES.has(candidate.status)) return "required";
  if (RECOVERY_OBSOLETE_STATUSES.has(candidate.status)) return "obsolete";
  return "unknown";
}

function isObsoleteRecoveryError(failure: unknown): boolean {
  return failure instanceof LocalApiError
    && ((failure.status === 404 && failure.code === "document_not_found")
      || (failure.status === 409 && failure.code === "reprocess_not_available"));
}

function LegacyReviewWorkspace({
  initialReprocessRecovery,
  onGroupedBatchesAvailable,
  onRecoveryObsolete,
  onReviewChanged,
}: {
  initialReprocessRecovery: LegacyReprocessRecovery | null;
  onGroupedBatchesAvailable: () => void;
  onRecoveryObsolete: () => void;
  onReviewChanged: () => void;
}): React.ReactElement {
  const { t } = useI18n();
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [reviewer, setReviewer] = useState("local-user");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [rejectionOpen, setRejectionOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [working, setWorking] = useState<"approve" | "reject" | "reprocess" | null>(null);
  const [reprocessRecovery, setReprocessRecovery] = useState<ReprocessRecovery | null>(() => initialReprocessRecovery
    ? { ...initialReprocessRecovery, readiness: "checking" }
    : null);
  const [modeProbePending, setModeProbePending] = useState(false);
  const [legacyAuthorityValid, setLegacyAuthorityValid] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await api.pendingReview();
      setItems(next);
      setSelectedId((current) => next.some((item) => item.extraction_id === current) ? current : next[0]?.extraction_id ?? null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("review.queue_unavailable"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!initialReprocessRecovery) return;
    let active = true;
    const { documentId } = initialReprocessRecovery;
    void api.status(documentId).then((document) => {
      if (!active) return;
      const disposition = recoveryDisposition(document, documentId);
      if (disposition === "required") {
        setReprocessRecovery((current) => current?.documentId === documentId
          ? { ...current, message: undefined, readiness: "ready" }
          : current);
        return;
      }
      if (disposition === "obsolete") {
        clearLegacyReprocessRecovery(documentId);
        setReprocessRecovery(null);
        onRecoveryObsolete();
        return;
      }
      setReprocessRecovery((current) => current?.documentId === documentId
        ? { ...current, message: t("original.identity_unavailable"), readiness: "unavailable" }
        : current);
    }).catch((failure: unknown) => {
      if (!active) return;
      if (isObsoleteRecoveryError(failure)) {
        clearLegacyReprocessRecovery(documentId);
        setReprocessRecovery(null);
        onRecoveryObsolete();
        return;
      }
      setReprocessRecovery((current) => current?.documentId === documentId
        ? { ...current, message: failure instanceof Error ? failure.message : t("review.queue_unavailable"), readiness: "unavailable" }
        : current);
    });
    return () => { active = false; };
  }, [initialReprocessRecovery, onRecoveryObsolete, t]);

  const reloadLegacy = async (): Promise<void> => {
    setModeProbePending(true);
    setLegacyAuthorityValid(false);
    setError(null);
    try {
      const grouped = await api.reviewBatches({ limit: 1, offset: 0 });
      if (!Array.isArray(grouped.items) || !Number.isInteger(grouped.total) || grouped.total < 0) {
        throw new Error(t("review.queue_unavailable"));
      }
      if (!reprocessRecovery && (grouped.items.length > 0 || grouped.total > 0)) {
        onGroupedBatchesAvailable();
        return;
      }
      setLegacyAuthorityValid(true);
      await load();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("review.queue_unavailable"));
    } finally {
      setModeProbePending(false);
    }
  };

  const item = useMemo(() => items.find((candidate) => candidate.extraction_id === selectedId) ?? null, [items, selectedId]);
  const draft = item ? drafts[item.extraction_id] ?? {} : {};
  const flaggedCount = items.reduce((total, current) => total + (current.flagged_fields.length > 0 ? 1 : 0), 0);
  const duplicates = items.reduce((total, current) => total + (current.duplicate_candidates.length > 0 ? 1 : 0), 0);

  const updateDraft = (key: string, value: string): void => {
    if (!item) return;
    setDrafts((current) => ({ ...current, [item.extraction_id]: { ...current[item.extraction_id], [key]: value } }));
  };

  const corrections = (): JsonRecord => {
    if (!item) return {};
    return Object.fromEntries(Object.entries(draft).filter(([key, value]) => isEditableCorrectionField(key, item.doc_class) && value !== "").map(([key, value]) => [key, parseEdit(value) as JsonValue]));
  };

  const approve = async (): Promise<void> => {
    if (!legacyAuthorityValid || modeProbePending) { setError(t("review.queue_unavailable")); return; }
    if (!item || !reviewer.trim()) { setError(t("review.reviewer_required")); return; }
    setWorking("approve"); setError(null); setStale(false);
    try {
      const response = await api.approve(item.extraction_id, item.version, corrections(), reviewer.trim());
      setNotice(t("review.verified", { verified_id: response.verified_id }));
      setDrafts((current) => { const next = { ...current }; delete next[item.extraction_id]; return next; });
      await load(); onReviewChanged();
    } catch (reason) {
      if (reason instanceof LocalApiError && reason.isStaleReview) setStale(true);
      else setError(reason instanceof Error ? reason.message : t("review.approval_failed"));
    } finally { setWorking(null); }
  };

  const rejectAndReprocess = async (): Promise<void> => {
    if (!legacyAuthorityValid || modeProbePending) { setError(t("review.queue_unavailable")); return; }
    if (!item || !reason.trim() || !confirmed || !reviewer.trim()) { setError(t("review.rejection_requirements")); return; }
    const rejectedItem = item;
    setWorking("reject"); setError(null); setNotice(null);
    try {
      await api.reject(rejectedItem.extraction_id, reason.trim(), reviewer.trim());
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("review.rejection_failed"));
      setWorking(null);
      return;
    }

    const remainingItems = items.filter((candidate) => candidate.extraction_id !== rejectedItem.extraction_id);
    setItems(remainingItems);
    setSelectedId((current) => current === rejectedItem.extraction_id ? remainingItems[0]?.extraction_id ?? null : current);
    setDrafts((current) => { const next = { ...current }; delete next[rejectedItem.extraction_id]; return next; });
    setReason(""); setConfirmed(false); setRejectionOpen(false);

    persistLegacyReprocessRecovery(rejectedItem.document_id);
    let queued: Awaited<ReturnType<typeof api.reprocess>>;
    try {
      queued = await api.reprocess(rejectedItem.document_id, reviewer.trim());
    } catch (failure) {
      setReprocessRecovery({
        documentId: rejectedItem.document_id,
        message: failure instanceof Error ? failure.message : t("review.reprocess_failed"),
        readiness: "ready",
      });
      await load();
      setWorking(null);
      return;
    }

    clearLegacyReprocessRecovery(rejectedItem.document_id);
    setReprocessRecovery(null);
    setNotice(queued.status === "already_queued" ? t("scan.already_queued") : t("review.rejected_queued"));
    await load();
    setWorking(null);
    onReviewChanged();
  };

  const retryRejectedReprocess = async (): Promise<void> => {
    if (!reprocessRecovery || !reviewer.trim()) { setError(t("review.reviewer_required")); return; }
    const recovery = reprocessRecovery;
    setWorking("reprocess"); setError(null); setNotice(null);
    try {
      const document = await api.status(recovery.documentId);
      const disposition = recoveryDisposition(document, recovery.documentId);
      if (disposition === "obsolete") {
        clearLegacyReprocessRecovery(recovery.documentId);
        setReprocessRecovery(null);
        setWorking(null);
        onRecoveryObsolete();
        return;
      }
      if (disposition !== "required") {
        setReprocessRecovery({ ...recovery, message: t("original.identity_unavailable"), readiness: "unavailable" });
        setWorking(null);
        return;
      }
    } catch (failure) {
      if (isObsoleteRecoveryError(failure)) {
        clearLegacyReprocessRecovery(recovery.documentId);
        setReprocessRecovery(null);
        setWorking(null);
        onRecoveryObsolete();
        return;
      }
      setReprocessRecovery({
        ...recovery,
        message: failure instanceof Error ? failure.message : t("review.queue_unavailable"),
        readiness: "unavailable",
      });
      setWorking(null);
      return;
    }
    let queued: Awaited<ReturnType<typeof api.reprocess>>;
    try {
      queued = await api.reprocess(recovery.documentId, reviewer.trim());
    } catch (failure) {
      if (isObsoleteRecoveryError(failure)) {
        clearLegacyReprocessRecovery(recovery.documentId);
        setReprocessRecovery(null);
        setWorking(null);
        onRecoveryObsolete();
        return;
      }
      setReprocessRecovery({
        ...recovery,
        message: failure instanceof Error ? failure.message : t("review.reprocess_failed"),
        readiness: "ready",
      });
      setWorking(null);
      return;
    }
    clearLegacyReprocessRecovery(recovery.documentId);
    setReprocessRecovery(null);
    setNotice(queued.status === "already_queued" ? t("scan.already_queued") : t("review.rejected_queued"));
    await load();
    setWorking(null);
    onReviewChanged();
  };

  const reprocess = async (): Promise<void> => {
    if (!legacyAuthorityValid || modeProbePending) { setError(t("review.queue_unavailable")); return; }
    if (!item || !reviewer.trim()) { setError(t("review.reviewer_required")); return; }
    setWorking("reprocess"); setError(null);
    try {
      await api.reprocess(item.document_id, reviewer.trim());
      setNotice(t("scan.queued_reprocess"));
    } catch (reason) { setError(reason instanceof Error ? reason.message : t("review.reprocess_failed")); }
    finally { setWorking(null); }
  };

  return <div className="page-stack">
    <PageHeading title={t("page.inbox.title")} copy={t("page.inbox.copy")} action={<Button className="button-secondary" onClick={() => void reloadLegacy()} disabled={modeProbePending || working !== null}><IconReload size={18} aria-hidden="true" />{t("review.reload_queue")}</Button>} />
    {notice ? <Notice tone="success" onDismiss={() => setNotice(null)}>{notice}</Notice> : null}
    {error ? <Notice tone="error" onDismiss={() => setError(null)}>{error}</Notice> : null}
    {reprocessRecovery ? <Notice tone="error"><div><p><strong>{t("documents.batch_state.rejected")}</strong>{" — "}{t("review.reprocess_failed")}</p>{reprocessRecovery.message && reprocessRecovery.message !== t("review.reprocess_failed") ? <p>{reprocessRecovery.message}</p> : null}<Button className="button-secondary" onClick={() => void retryRejectedReprocess()} disabled={working !== null || reprocessRecovery.readiness === "checking"}><IconReload size={17} aria-hidden="true" />{working === "reprocess" ? t("action.queueing") : t("scan.reprocess")}</Button></div></Notice> : null}
    {stale ? <Notice tone="warning"><strong>{t("review.changed_conflict")}</strong><Button className="button-secondary" onClick={() => { setStale(false); void reloadLegacy(); }}><IconReload size={17} />{t("review.reload_item")}</Button></Notice> : null}
    <section className="metrics-strip" aria-label={t("page.inbox.title")}><div><span>{t("review.in_queue")}</span><strong>{items.length}</strong></div><div><span>{t("review.need_attention")}</span><strong>{flaggedCount}</strong></div><div><span>{t("review.possible_duplicates")}</span><strong>{duplicates}</strong></div></section>
    {loading ? <LoadingPanel label={t("loading.review_queue")} /> : null}
    {!loading && items.length === 0 ? <EmptyState title={t("review.empty_title")} copy={t("review.empty_copy")} /> : null}
    {item ? <section className="review-shell">
      <div className="review-toolbar">
        <label><span>{t("review.reviewer")}</span><input value={reviewer} disabled={modeProbePending || !legacyAuthorityValid} onChange={(event) => setReviewer(event.target.value)} aria-invalid={!reviewer.trim()} /></label>
        <label><span>{t("review.document")}</span><select value={item.extraction_id} disabled={modeProbePending || !legacyAuthorityValid} onChange={(event) => { setSelectedId(event.target.value); setStale(false); }}>
          {items.map((candidate) => <option key={candidate.extraction_id} value={candidate.extraction_id}>{reviewTitle(candidate)}</option>)}
        </select></label>
      </div>
      <div className="review-grid">
        <OriginalPreview key={`${item.document_id}:${item.extraction_id}:${item.source_file_id}:${item.source_version}`} item={item} />
        <section className="field-pane" aria-labelledby="fields-title">
          <div className="panel-heading"><div><span className="eyebrow">{t("review.extraction")}</span><h2 id="fields-title">{t("field.details")}</h2></div><span className={item.flagged_fields.length ? "review-state needs-attention" : "review-state"}>{item.flagged_fields.length ? t("review.need_attention") : t("review.ready_to_verify")}</span></div>
          <p className="panel-copy">{t("field.details_copy")}</p>
          {item.flagged_fields.length ? <Notice tone="warning">{t("review.fields_first", { fields: item.flagged_fields.join(", ") })}</Notice> : null}
          <Tabs.Root defaultValue="fields">
          <Tabs.List className="review-tabs" aria-label={t("field.details")}>
            <Tabs.Trigger value="fields">{t("field.details")}</Tabs.Trigger>
            <Tabs.Trigger value="evidence">{t("original.evidence")}</Tabs.Trigger>
          </Tabs.List>
          <Tabs.Content value="fields" className="field-list">
            {Object.entries(item.suggested).map(([key, rawValue]) => {
              const confidence = fieldConfidence(rawValue); const source = fieldSource(rawValue) ?? (typeof item.source_spans[key] === "string" ? item.source_spans[key] : undefined); const flagged = needsReview(key, item.flagged_fields); const defaultValue = editValue(rawValue);
              const editable = isEditableCorrectionField(key, item.doc_class);
              return <article className={flagged ? "review-field is-flagged" : "review-field"} key={key}>
                <div className="field-topline"><label htmlFor={`field-${key}`}>{fieldLabel(key, t)}</label><span className={flagged ? "field-badge field-badge-warning" : "field-badge"}>{flagged ? t("field.needs_review") : confidence === undefined ? t("field.extracted") : `${Math.round(confidence * 100)}%`}</span></div>
                {editable ? (defaultValue.length > 150 ? <textarea id={`field-${key}`} value={draft[key] ?? defaultValue} onChange={(event) => updateDraft(key, event.target.value)} /> : <input id={`field-${key}`} value={draft[key] ?? defaultValue} onChange={(event) => updateDraft(key, event.target.value)} />) : <output className="field-readonly" aria-readonly="true">{defaultValue || t("value.unspecified")}</output>}
                {source ? <span className="field-source">{t("field.source", { source })}</span> : null}
              </article>;
            })}
          </Tabs.Content>
          <Tabs.Content value="evidence" className="source-spans"><pre>{JSON.stringify(item.source_spans, null, 2)}</pre></Tabs.Content>
          </Tabs.Root>
        </section>
        <aside className="action-pane" aria-label={t("review.controls_title")}>
          <div><span className="eyebrow">{t("review.decision")}</span><h2>{t("review.controls_title")}</h2><p>{t("review.controls_copy")}</p></div>
          {item.duplicate_candidates.length ? <details className="duplicate-details"><summary><IconCopyCheck size={17} />{t("duplicate.evidence")}<IconChevronRight size={17} /></summary><pre>{JSON.stringify(item.duplicate_candidates, null, 2)}</pre></details> : null}
          <Button className="button-primary" onClick={() => void approve()} disabled={working !== null || modeProbePending || !legacyAuthorityValid || reprocessRecovery !== null}><IconSend size={18} aria-hidden="true" />{working === "approve" ? t("action.approving") : t("review.approve")}</Button>
          <Button className="button-secondary" onClick={() => setRejectionOpen((current) => !current)} disabled={working !== null || modeProbePending || !legacyAuthorityValid || reprocessRecovery !== null}><IconX size={18} aria-hidden="true" />{t("review.reject")}</Button>
          <Button className="button-quiet" onClick={() => void reprocess()} disabled={working !== null || modeProbePending || !legacyAuthorityValid || reprocessRecovery !== null}><IconReload size={18} aria-hidden="true" />{working === "reprocess" ? t("action.queueing") : t("scan.reprocess")}</Button>
          {rejectionOpen ? <div className="rejection-form"><label>{t("review.rejection_reason")}<textarea value={reason} disabled={modeProbePending || !legacyAuthorityValid} onChange={(event) => setReason(event.target.value)} /></label><label className="check-row"><input type="checkbox" checked={confirmed} disabled={modeProbePending || !legacyAuthorityValid} onChange={(event) => setConfirmed(event.target.checked)} />{t("review.confirm_rejection")}</label><Button className="button-danger" onClick={() => void rejectAndReprocess()} disabled={working !== null || modeProbePending || !legacyAuthorityValid || !confirmed}>{working === "reject" ? t("action.rejecting") : t("review.reject_reprocess")}</Button></div> : null}
        </aside>
      </div>
    </section> : null}
  </div>;
}

export function ReviewWorkspace({ onReviewChanged }: { onReviewChanged: () => void }): React.ReactElement {
  const [initialReprocessRecovery, setInitialReprocessRecovery] = useState(readLegacyReprocessRecovery);
  const [useLegacy, setUseLegacy] = useState(initialReprocessRecovery !== null);
  const clearObsoleteRecovery = useCallback(() => {
    setInitialReprocessRecovery(null);
    setUseLegacy(false);
  }, []);
  return useLegacy
    ? <LegacyReviewWorkspace initialReprocessRecovery={initialReprocessRecovery} onGroupedBatchesAvailable={() => setUseLegacy(false)} onRecoveryObsolete={clearObsoleteRecovery} onReviewChanged={onReviewChanged} />
    : <GroupedReviewWorkspace onReviewChanged={onReviewChanged} onEmpty={() => setUseLegacy(true)} />;
}

export default ReviewWorkspace;
