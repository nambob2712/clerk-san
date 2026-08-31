import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { IconBolt, IconChevronLeft, IconChevronRight, IconReload, IconSend, IconX } from "@tabler/icons-react";

import type {
  ActivationPreview,
  ExactSourcePreview,
  FinancialSubtype,
  JsonRecord,
  ReviewBatchSummary,
  ReviewCandidate,
  ReviewCandidateDecision,
  ReviewDuplicateEvidence,
  SourceIntakeDetail,
} from "@/api/contracts";
import { api, LocalApiError } from "@/api/client";
import OriginalPreview from "@/components/original-preview";
import { Button, EmptyState, LoadingPanel, Notice, PageHeading } from "@/components/ui";
import ReviewCandidateTable, { type CandidateDecisionDraft } from "@/features/review/review-candidate-table";
import { useI18n, type TranslationKey } from "@/lib/i18n";

const PAGE_SIZE = 50;
const BATCH_PAGE_SIZE = 50;
const BATCH_LIFECYCLE_KEYS: Readonly<Record<ReviewBatchSummary["lifecycle"], TranslationKey>> = {
  open: "documents.batch_state.open",
  ready_to_activate: "documents.batch_state.ready_to_activate",
  active: "documents.batch_state.active",
  superseded: "documents.batch_state.superseded",
  rejected: "documents.batch_state.rejected",
};

function isLowerSha256(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/u.test(value);
}

function parseCorrections(text: string): JsonRecord | null {
  if (!text.trim()) return null;
  const value = JSON.parse(text) as unknown;
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("corrections_not_object");
  return value as JsonRecord;
}

function affectedIds(error: LocalApiError): string[] {
  const value = error.detail?.affected_extraction_ids;
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

export function GroupedReviewWorkspace({ onReviewChanged, onEmpty }: { onReviewChanged: () => void; onEmpty: () => void }): React.ReactElement {
  const { t } = useI18n();
  const [batches, setBatches] = useState<ReviewBatchSummary[]>([]);
  const [batchOffset, setBatchOffset] = useState(0);
  const [batchTotal, setBatchTotal] = useState(0);
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<ReviewCandidate[]>([]);
  const [candidateTotal, setCandidateTotal] = useState(0);
  const [batchVersion, setBatchVersion] = useState(1);
  const [offset, setOffset] = useState(0);
  const [exceptionOnly, setExceptionOnly] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, CandidateDecisionDraft>>({});
  const [staleIds, setStaleIds] = useState<Set<string>>(new Set());
  const [source, setSource] = useState<ExactSourcePreview | null>(null);
  const [sourceDuplicateEvidence, setSourceDuplicateEvidence] = useState<ReviewDuplicateEvidence[]>([]);
  const [actor, setActor] = useState("local-user");
  const [preview, setPreview] = useState<ActivationPreview | null>(null);
  const [acceptExclusions, setAcceptExclusions] = useState(false);
  const [acceptEmpty, setAcceptEmpty] = useState(false);
  const [reprocessReason, setReprocessReason] = useState("");
  const [confirmedReprocessFor, setConfirmedReprocessFor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState<"decisions" | "preview" | "activate" | "reprocess" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [restorePreviewFocus, setRestorePreviewFocus] = useState(false);
  const emptyReported = useRef(false);
  const batchRequest = useRef(0);
  const pageRequest = useRef(0);
  const previewRequest = useRef(0);
  const previewTrigger = useRef<HTMLButtonElement>(null);
  const intakeCache = useRef(new Map<string, Promise<SourceIntakeDetail>>());
  const sourceCache = useRef(new Map<string, Promise<ExactSourcePreview | null>>());
  const selectedBatchIdRef = useRef(selectedBatchId);
  const tRef = useRef(t);
  const onEmptyRef = useRef(onEmpty);
  selectedBatchIdRef.current = selectedBatchId;
  tRef.current = t;
  onEmptyRef.current = onEmpty;

  const selectedBatch = useMemo(() => batches.find((batch) => batch.id === selectedBatchId) ?? null, [batches, selectedBatchId]);
  const hasDrafts = Object.keys(drafts).length > 0;

  const clearBatchBoundState = useCallback(() => {
    pageRequest.current += 1;
    previewRequest.current += 1;
    setCandidates([]);
    setCandidateTotal(0);
    setBatchVersion(1);
    setOffset(0);
    setSource(null);
    setSourceDuplicateEvidence([]);
    setPreview(null);
    setAcceptExclusions(false);
    setAcceptEmpty(false);
    setReprocessReason("");
    setConfirmedReprocessFor(null);
    setDrafts({});
    setStaleIds(new Set());
    setError(null);
    setRestorePreviewFocus(false);
  }, []);

  const replaceSelectedBatch = useCallback((nextBatchId: string | null) => {
    if (selectedBatchIdRef.current === nextBatchId) return;
    clearBatchBoundState();
    selectedBatchIdRef.current = nextBatchId;
    setSelectedBatchId(nextBatchId);
  }, [clearBatchBoundState]);

  const loadBatches = useCallback(async () => {
    const requestId = ++batchRequest.current;
    setLoading(true);
    setRefreshError(null);
    try {
      let response = await api.reviewBatches({ limit: BATCH_PAGE_SIZE, offset: batchOffset });
      if (batchRequest.current !== requestId) return;
      let responseOffset = batchOffset;
      const currentBatchId = selectedBatchIdRef.current;
      if (currentBatchId && !response.items.some((batch) => batch.id === currentBatchId) && response.total > 0) {
        const searchLimit = response.total;
        for (let searchOffset = 0; searchOffset < searchLimit; searchOffset += BATCH_PAGE_SIZE) {
          if (searchOffset === batchOffset) continue;
          const candidatePage = await api.reviewBatches({ limit: BATCH_PAGE_SIZE, offset: searchOffset });
          if (batchRequest.current !== requestId) return;
          if (candidatePage.items.some((batch) => batch.id === currentBatchId)) {
            response = candidatePage;
            responseOffset = searchOffset;
            break;
          }
        }
      }
      setBatchTotal(response.total);
      if (response.total === 0) {
        replaceSelectedBatch(null);
        setBatches([]);
        if (!emptyReported.current) {
          emptyReported.current = true;
          onEmptyRef.current();
        }
        return;
      }
      if (currentBatchId && !response.items.some((batch) => batch.id === currentBatchId)) {
        replaceSelectedBatch(null);
        setBatches(response.items);
        setError(`${tRef.current("batch.changed")}. ${tRef.current("review.reload_queue")}.`);
        return;
      }
      if (response.items.length === 0 && response.total > 0 && batchOffset > 0) {
        replaceSelectedBatch(null);
        setBatches([]);
        setBatchOffset(Math.max(0, Math.floor((response.total - 1) / BATCH_PAGE_SIZE) * BATCH_PAGE_SIZE));
        return;
      }
      const nextBatchId = currentBatchId ?? response.items[0]?.id ?? null;
      replaceSelectedBatch(nextBatchId);
      setBatches(response.items);
      if (responseOffset !== batchOffset) setBatchOffset(responseOffset);
    } catch (reason) {
      if (batchRequest.current === requestId) setRefreshError(reason instanceof Error ? reason.message : tRef.current("batch.load_failed"));
    } finally {
      if (batchRequest.current === requestId) setLoading(false);
    }
  }, [batchOffset, replaceSelectedBatch]);

  const changeBatchPage = (nextOffset: number): void => {
    if (loading || working !== null || hasDrafts || nextOffset < 0 || nextOffset >= batchTotal) return;
    batchRequest.current += 1;
    clearBatchBoundState();
    selectedBatchIdRef.current = null;
    setSelectedBatchId(null);
    setBatches([]);
    setLoading(true);
    setBatchOffset(nextOffset);
  };

  useEffect(() => { void loadBatches(); }, [loadBatches]);

  useEffect(() => {
    if (restorePreviewFocus && working === null) {
      previewTrigger.current?.focus();
      setRestorePreviewFocus(false);
    }
  }, [restorePreviewFocus, working]);

  const loadPage = useCallback(async (preserveDrafts = false) => {
    if (!selectedBatchId || selectedBatchIdRef.current !== selectedBatchId) return;
    const batchId = selectedBatchId;
    const requestId = ++pageRequest.current;
    setLoading(true);
    setError(null);
    setCandidates([]);
    setCandidateTotal(0);
    setSourceDuplicateEvidence([]);
    setPreview(null);
    try {
      const batch = batches.find((candidate) => candidate.id === batchId);
      const intakeKey = batch ? `${batch.source_intake_id}:${batch.document_id}:${batch.source_file_id}:${batch.source_version}` : null;
      let intakePromise = intakeKey ? intakeCache.current.get(intakeKey) : undefined;
      if (batch && intakeKey && !intakePromise) {
        intakePromise = api.intake(batch.source_intake_id).then((intake) => {
          if (
            intake.intake_id !== batch.source_intake_id
            || intake.document_id !== batch.document_id
            || intake.source_file_id !== batch.source_file_id
            || intake.source_version !== batch.source_version
          ) throw new Error(tRef.current("original.identity_unavailable"));
          return intake;
        }).catch((reason) => {
          intakeCache.current.delete(intakeKey);
          throw reason;
        });
        intakeCache.current.set(intakeKey, intakePromise);
      }
      const exactSourcePromise = batch && intakePromise
        ? intakePromise.then((intake) => {
          if (!isLowerSha256(intake.source_sha256)) return null;
          const sourceKey = `${batch.document_id}:${batch.source_file_id}:${batch.source_version}:${intake.source_sha256}`;
          let cached = sourceCache.current.get(sourceKey);
          if (!cached) {
            cached = api.document(batch.document_id).then((document) => {
              const file = document.files.find((candidate) => candidate.kind === "original" && candidate.id === batch.source_file_id && candidate.version === batch.source_version);
              const exactSource = document.id === batch.document_id && isLowerSha256(file?.sha256) && file.sha256 === intake.source_sha256
                ? { document_id: batch.document_id, source_intake_id: batch.source_intake_id, source_file_id: batch.source_file_id, source_version: batch.source_version, source_sha256: intake.source_sha256, filename: file.source_filename ?? document.source_filename, mime: file.mime ?? "", created_at: document.created_at }
                : null;
              if (!exactSource) sourceCache.current.delete(sourceKey);
              return exactSource;
            }).catch((reason) => {
              sourceCache.current.delete(sourceKey);
              throw reason;
            });
            sourceCache.current.set(sourceKey, cached);
          }
          return cached;
        })
        : Promise.resolve(null);
      const [page, exactSource] = await Promise.all([
        api.reviewCandidates(batchId, PAGE_SIZE, offset, exceptionOnly),
        exactSourcePromise,
      ]);
      if (pageRequest.current !== requestId || page.batch_id !== batchId) return;
      setSource(exactSource);
      if (!exactSource) setError(tRef.current("original.identity_unavailable"));
      setCandidates(page.items);
      setCandidateTotal(page.total);
      setBatchVersion(page.batch_version);
      setSourceDuplicateEvidence(page.source_duplicate_evidence);
      if (!preserveDrafts) setDrafts({});
    } catch (reason) {
      if (pageRequest.current === requestId) setError(reason instanceof Error ? reason.message : tRef.current("batch.candidates_failed"));
    } finally {
      if (pageRequest.current === requestId) setLoading(false);
    }
  }, [batches, exceptionOnly, offset, selectedBatchId]);

  useEffect(() => { void loadPage(false); }, [loadPage]);

  const stageDecisions = async (): Promise<void> => {
    if (!selectedBatchId || !actor.trim()) { setError(t("review.reviewer_required")); return; }
    const edited = candidates.filter((candidate) => drafts[candidate.extraction_id]);
    if (edited.length === 0) { setError(t("batch.no_drafts")); return; }
    let decisions: ReviewCandidateDecision[];
    try {
      decisions = edited.map((candidate) => {
        const draft = drafts[candidate.extraction_id];
        if (!draft.action) throw new Error(t("batch.decision_required"));
        if (draft.action === "exclude" && !draft.exclusionReason.trim()) throw new Error(t("batch.reason_required"));
        const corrections = draft.action === "include" ? parseCorrections(draft.correctionsText) : null;
        return {
          extraction_id: candidate.extraction_id,
          expected_extraction_version: candidate.version,
          expected_decision_revision: candidate.latest_decision?.decision_revision ?? 0,
          action: draft.action,
          corrections,
          corrected_financial_subtype: draft.action === "include" && draft.correctedFinancialSubtype ? draft.correctedFinancialSubtype as FinancialSubtype : null,
          exclusion_reason: draft.action === "exclude" ? draft.exclusionReason.trim() : null,
        };
      });
    } catch (reason) {
      setError(reason instanceof Error && reason.message === "corrections_not_object" ? t("validation.corrections_json_object") : reason instanceof Error ? reason.message : t("batch.invalid_draft"));
      return;
    }
    setWorking("decisions"); setError(null); setStaleIds(new Set());
    try {
      const result = await api.decideReviewBatch(selectedBatchId, { expected_batch_version: batchVersion, decisions, actor: actor.trim() });
      setNotice(t("batch.decisions_saved", { count: result.decisions.length }));
      setDrafts({});
      await loadBatches();
      await loadPage(false);
    } catch (reason) {
      if (reason instanceof LocalApiError && reason.isStaleBatch) {
        const changedIds = new Set(affectedIds(reason));
        await loadPage(true);
        setStaleIds(changedIds);
        setError(t("batch.stale_drafts_preserved"));
      } else setError(reason instanceof Error ? reason.message : t("batch.decisions_failed"));
    } finally { setWorking(null); }
  };

  const loadActivationPreview = async (): Promise<void> => {
    if (!selectedBatchId) return;
    const batchId = selectedBatchId;
    const requestId = ++previewRequest.current;
    setWorking("preview"); setError(null);
    try {
      const next = await api.activationPreview(batchId);
      if (previewRequest.current !== requestId || next.batch_id !== batchId) return;
      setPreview(next);
      setBatchVersion(next.batch_version);
      setAcceptExclusions(false);
      setAcceptEmpty(false);
    } catch (reason) {
      if (previewRequest.current === requestId) setError(reason instanceof Error ? reason.message : t("batch.preview_failed"));
    } finally {
      if (previewRequest.current === requestId) setWorking(null);
    }
  };

  const activate = async (): Promise<void> => {
    if (!actor.trim()) { setError(t("review.reviewer_required")); return; }
    if (!selectedBatchId || !preview || preview.batch_id !== selectedBatchId) return;
    setWorking("activate"); setError(null);
    try {
      const result = await api.activateReviewBatch(selectedBatchId, { expected_batch_version: preview.batch_version, expected_vector_sha256: preview.activation_vector_sha256, actor: actor.trim(), accept_exclusions: acceptExclusions, accept_empty: acceptEmpty });
      setNotice(t("batch.activated", { included: result.included_count, excluded: result.excluded_count }));
      setPreview(null);
      await loadBatches();
      await loadPage(false);
      onReviewChanged();
    } catch (reason) {
      if (reason instanceof LocalApiError && reason.isStaleBatch) {
        setPreview(null);
        await loadPage(true);
        setError(t("batch.activation_stale"));
        setRestorePreviewFocus(true);
      } else setError(reason instanceof Error ? reason.message : t("batch.activation_failed"));
    } finally { setWorking(null); }
  };

  const rejectAndReprocess = async (): Promise<void> => {
    const confirmationKey = selectedBatchId ? `${selectedBatchId}:${batchVersion}` : null;
    if (!selectedBatchId || !actor.trim() || !reprocessReason.trim() || confirmedReprocessFor !== confirmationKey) { setError(t("batch.reprocess_requirements")); return; }
    setWorking("reprocess"); setError(null);
    try {
      await api.rejectAndReprocessBatch(selectedBatchId, batchVersion, reprocessReason.trim(), actor.trim());
      setNotice(t("batch.reprocess_queued")); setReprocessReason(""); setConfirmedReprocessFor(null); setPreview(null);
      await loadBatches(); await loadPage(false); onReviewChanged();
    } catch (reason) {
      if (reason instanceof LocalApiError && reason.isStaleBatch) {
        await loadPage(true);
        setError(t("batch.stale_drafts_preserved"));
      } else setError(reason instanceof Error ? reason.message : t("batch.reprocess_failed"));
    } finally { setWorking(null); }
  };

  return <div className="page-stack">
    <PageHeading title={t("batch.title")} copy={t("batch.copy")} action={<Button className="button-secondary" onClick={() => void loadBatches()} disabled={loading || working !== null || hasDrafts}><IconReload size={18} />{t("review.reload_queue")}</Button>} />
    {notice ? <Notice tone="success" onDismiss={() => setNotice(null)}>{notice}</Notice> : null}
    {refreshError ? <Notice tone="error" onDismiss={() => setRefreshError(null)}>{refreshError}</Notice> : null}
    {error ? <Notice tone="error" onDismiss={() => setError(null)}>{error}</Notice> : null}
    {hasDrafts ? <Notice tone="warning"><span>{t("batch.navigation_locked")}</span><Button className="button-quiet" disabled={working !== null} onClick={() => { setDrafts({}); setStaleIds(new Set()); setPreview(null); }}>{t("batch.discard_drafts")}</Button></Notice> : null}
    {loading ? <LoadingPanel label={t("loading.review_queue")} /> : null}
    {batchTotal > BATCH_PAGE_SIZE ? <div className="pagination" role="group" aria-label={`${t("batch.source_batch")} · ${t("batch.page_range", { from: batches.length ? batchOffset + 1 : 0, to: batches.length ? Math.min(batchOffset + batches.length, batchTotal) : 0, total: batchTotal })}`}><Button className="button-secondary" disabled={batchOffset === 0 || loading || working !== null || hasDrafts} onClick={() => changeBatchPage(Math.max(0, batchOffset - BATCH_PAGE_SIZE))}><IconChevronLeft size={18} />{t("action.previous")}</Button><span>{t("batch.page_range", { from: batches.length ? batchOffset + 1 : 0, to: batches.length ? Math.min(batchOffset + batches.length, batchTotal) : 0, total: batchTotal })}</span><Button className="button-secondary" disabled={batchOffset + BATCH_PAGE_SIZE >= batchTotal || loading || working !== null || hasDrafts} onClick={() => changeBatchPage(batchOffset + BATCH_PAGE_SIZE)}>{t("action.next")}<IconChevronRight size={18} /></Button></div> : null}
    {!loading && batches.length === 0 ? <EmptyState title={t("review.empty_title")} copy={t("review.empty_copy")} /> : null}
    {selectedBatch ? <>
      <section className="batch-toolbar"><label><span>{t("review.reviewer")}</span><input value={actor} aria-invalid={!actor.trim()} disabled={working !== null} onChange={(event) => setActor(event.target.value)} /></label><label><span>{t("batch.source_batch")}</span><select value={selectedBatch.id} disabled={working !== null || hasDrafts} onChange={(event) => replaceSelectedBatch(event.target.value)} >{batches.map((batch) => <option key={batch.id} value={batch.id}>{batch.document_id.slice(0, 8)} · v{batch.source_version} · {batch.id.slice(0, 8)} · {batch.source_intake_id.slice(0, 8)} · {t(BATCH_LIFECYCLE_KEYS[batch.lifecycle])} · {batch.candidate_count}</option>)}</select></label><label className="check-row"><input type="checkbox" checked={exceptionOnly} disabled={working !== null || hasDrafts} onChange={(event) => { pageRequest.current += 1; setExceptionOnly(event.target.checked); setOffset(0); setCandidates([]); setCandidateTotal(0); setSourceDuplicateEvidence([]); setDrafts({}); setPreview(null); }} />{t("batch.exceptions_first")}</label></section>
      <section className="metrics-strip" aria-label={t("batch.progress")}><div><span>{t("batch.pending")}</span><strong>{selectedBatch.pending_count}</strong></div><div><span>{t("batch.included")}</span><strong>{selectedBatch.included_count}</strong></div><div><span>{t("batch.excluded")}</span><strong>{selectedBatch.excluded_count}</strong></div></section>
      <Notice tone={selectedBatch.lifecycle === "active" ? "success" : "info"}>{selectedBatch.lifecycle === "active" ? t("batch.authoritative") : t("batch.staged_not_authoritative")}</Notice>
      {sourceDuplicateEvidence.length ? <Notice tone="warning"><details className="batch-disclosure"><summary>{t("batch.source_duplicate_summary", { count: sourceDuplicateEvidence.length })}</summary><pre>{JSON.stringify(sourceDuplicateEvidence, null, 2)}</pre></details></Notice> : null}
      <section className="grouped-review-layout">{source ? <OriginalPreview source={source} /> : null}<div className="candidate-pane"><ReviewCandidateTable candidates={candidates} drafts={drafts} staleIds={staleIds} disabled={working !== null} onDraftChange={(candidate, draft) => { setDrafts((current) => ({ ...current, [candidate.extraction_id]: draft })); setPreview(null); }} /><div className="pagination"><Button className="button-secondary" disabled={offset === 0 || working !== null || hasDrafts} onClick={() => { pageRequest.current += 1; setOffset((current) => Math.max(0, current - PAGE_SIZE)); setCandidates([]); setCandidateTotal(0); setSourceDuplicateEvidence([]); setPreview(null); }}><IconChevronLeft size={18} />{t("action.previous")}</Button><span>{t("batch.page_range", { from: candidateTotal ? offset + 1 : 0, to: Math.min(offset + PAGE_SIZE, candidateTotal), total: candidateTotal })}</span><Button className="button-secondary" disabled={offset + PAGE_SIZE >= candidateTotal || working !== null || hasDrafts} onClick={() => { pageRequest.current += 1; setOffset((current) => current + PAGE_SIZE); setCandidates([]); setCandidateTotal(0); setSourceDuplicateEvidence([]); setPreview(null); }}>{t("action.next")}<IconChevronRight size={18} /></Button></div></div></section>
      {selectedBatch.lifecycle !== "active" && selectedBatch.lifecycle !== "rejected" && selectedBatch.lifecycle !== "superseded" ? <section className="batch-actions"><div><h2>{t("batch.stage_title")}</h2><p>{t("batch.stage_copy")}</p><Button className="button-secondary" onClick={() => void stageDecisions()} disabled={working !== null || !hasDrafts}><IconSend size={18} />{working === "decisions" ? t("batch.saving") : t("batch.save_decisions")}</Button></div><div><h2>{t("batch.activate_title")}</h2><p>{t("batch.activate_copy")}</p><Button ref={previewTrigger} className="button-secondary" onClick={() => void loadActivationPreview()} disabled={working !== null || hasDrafts}><IconBolt size={18} />{working === "preview" ? t("batch.previewing") : t("batch.preview_activation")}</Button>{preview ? <div className="activation-confirm"><dl><div><dt>{t("batch.total")}</dt><dd>{preview.total_count}</dd></div><div><dt>{t("batch.pending")}</dt><dd>{preview.pending_count}</dd></div><div><dt>{t("batch.included")}</dt><dd>{preview.included_count}</dd></div><div><dt>{t("batch.excluded")}</dt><dd>{preview.excluded_count}</dd></div><div><dt>{t("batch.errors")}</dt><dd>{preview.error_count}</dd></div><div><dt>{t("batch.source_current")}</dt><dd>{preview.source_is_current ? t("batch.yes") : t("batch.no")}</dd></div><div><dt>{t("batch.counts_match")}</dt><dd>{preview.candidate_count_matches ? t("batch.yes") : t("batch.no")}</dd></div></dl><details className="batch-disclosure"><summary>{t("batch.reconciliation")}</summary><pre>{JSON.stringify({ counts: preview.reconciliation_counts, digest: preview.reconciliation_digest }, null, 2)}</pre></details>{preview.errors.length ? <Notice tone="error"><strong>{t("batch.activation_blocked")}</strong><ul>{preview.errors.map((item, index) => <li key={`${String(item.code ?? "activation_error")}:${index}`}><code>{String(item.code ?? "activation_error")}</code>{typeof item.message === "string" ? ` — ${item.message}` : ""}</li>)}</ul></Notice> : null}{preview.requires_accept_exclusions ? <label className="check-row"><input type="checkbox" checked={acceptExclusions} disabled={working !== null} onChange={(event) => setAcceptExclusions(event.target.checked)} />{t("batch.accept_exclusions")}</label> : null}{preview.requires_accept_empty ? <label className="check-row"><input type="checkbox" checked={acceptEmpty} disabled={working !== null} onChange={(event) => setAcceptEmpty(event.target.checked)} />{t("batch.accept_empty")}</label> : null}<Button className="button-primary" onClick={() => void activate()} disabled={working !== null || !preview.ready_for_activation || (preview.requires_accept_exclusions && !acceptExclusions) || (preview.requires_accept_empty && !acceptEmpty)}><IconBolt size={18} />{working === "activate" ? t("batch.activating") : t("batch.activate")}</Button></div> : null}</div><div><h2>{t("batch.reject_title")}</h2><p>{t("batch.reject_copy")}</p><label><span>{t("review.rejection_reason")}</span><textarea value={reprocessReason} disabled={working !== null || hasDrafts} onChange={(event) => { setReprocessReason(event.target.value); setConfirmedReprocessFor(null); }} /></label><label className="check-row"><input type="checkbox" checked={confirmedReprocessFor === `${selectedBatch.id}:${batchVersion}`} disabled={working !== null || hasDrafts} onChange={(event) => setConfirmedReprocessFor(event.target.checked ? `${selectedBatch.id}:${batchVersion}` : null)} />{t("batch.confirm_reprocess")}</label><Button className="button-danger" onClick={() => void rejectAndReprocess()} disabled={working !== null || hasDrafts || confirmedReprocessFor !== `${selectedBatch.id}:${batchVersion}`}><IconX size={18} />{working === "reprocess" ? t("action.queueing") : t("review.reject_reprocess")}</Button></div></section> : null}
    </> : null}
  </div>;
}

export default GroupedReviewWorkspace;
