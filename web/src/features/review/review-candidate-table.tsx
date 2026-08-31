import type { CandidateDecisionAction, FinancialSubtype, RecordKind, ReviewCandidate } from "@/api/contracts";
import { useI18n, type TranslationKey } from "@/lib/i18n";

const FINANCIAL_SUBTYPES: FinancialSubtype[] = ["transaction", "receipt", "invoice", "bill", "recurring_bill", "quote", "other_financial"];
const FINANCIAL_SUBTYPE_KEYS: Readonly<Record<FinancialSubtype, TranslationKey>> = {
  transaction: "financial_subtype.transaction",
  receipt: "financial_subtype.receipt",
  invoice: "financial_subtype.invoice",
  bill: "financial_subtype.bill",
  recurring_bill: "financial_subtype.recurring_bill",
  quote: "financial_subtype.quote",
  other_financial: "financial_subtype.other_financial",
};
const RECORD_KIND_KEYS: Readonly<Record<RecordKind, TranslationKey>> = {
  financial: "mapping.financial",
  generic_document: "mapping.generic",
};

export interface CandidateDecisionDraft {
  action: CandidateDecisionAction | "";
  exclusionReason: string;
  correctionsText: string;
  correctedFinancialSubtype: FinancialSubtype | "";
}

export function candidateHasException(candidate: ReviewCandidate): boolean {
  return candidate.validation_issues.length > 0
    || candidate.duplicate_evidence.length > 0
    || candidate.evidence_group_keys.length > 0;
}

export function ReviewCandidateTable({
  candidates,
  drafts,
  staleIds,
  onDraftChange,
  disabled = false,
}: {
  candidates: ReviewCandidate[];
  drafts: Record<string, CandidateDecisionDraft>;
  staleIds: ReadonlySet<string>;
  onDraftChange: (candidate: ReviewCandidate, draft: CandidateDecisionDraft) => void;
  disabled?: boolean;
}): React.ReactElement {
  const { t } = useI18n();
  return <div className="data-table-wrap review-candidate-table"><table>
    <caption className="sr-only">{t("batch.candidate_table")}</caption>
    <thead><tr><th>{t("batch.candidate")}</th><th>{t("batch.suggested_data")}</th><th>{t("batch.exceptions")}</th><th>{t("batch.decision")}</th></tr></thead>
    <tbody>{candidates.map((candidate) => {
      const persisted = candidate.latest_decision;
      const draft = drafts[candidate.extraction_id] ?? {
        action: persisted?.action ?? "",
        exclusionReason: persisted?.exclusion_reason ?? "",
        correctionsText: persisted?.corrections ? JSON.stringify(persisted.corrections, null, 2) : "",
        correctedFinancialSubtype: persisted?.corrected_financial_subtype ?? "",
      };
      return <tr key={candidate.extraction_id} className={staleIds.has(candidate.extraction_id) ? "is-stale" : undefined}>
        <td><strong>#{candidate.candidate_ordinal}</strong><span className="candidate-meta">{t(RECORD_KIND_KEYS[candidate.record_kind])}{candidate.financial_subtype ? ` · ${candidate.financial_subtype}` : ""}</span><span className="candidate-meta">{candidate.source_locator}</span>{candidate.row_fingerprint ? <span className="candidate-meta">{t("batch.row_fingerprint")}: <code>{candidate.row_fingerprint.slice(0, 12)}</code></span> : null}<span className="candidate-meta">v{candidate.version} · {candidate.extraction_id.slice(0, 8)}</span>{staleIds.has(candidate.extraction_id) ? <span className="field-badge field-badge-warning">{t("batch.changed")}</span> : null}</td>
        <td><details><summary>{t("batch.show_values")}</summary><pre>{JSON.stringify(candidate.payload, null, 2)}</pre></details><label className="compact-field"><span>{t("batch.corrections_json")}</span><textarea value={draft.correctionsText} placeholder="{}" disabled={disabled} onChange={(event) => onDraftChange(candidate, { ...draft, correctionsText: event.target.value })} /></label>{candidate.record_kind === "financial" ? <label className="compact-field"><span>{t("mapping.financial_subtype")}</span><select value={draft.correctedFinancialSubtype} disabled={disabled} onChange={(event) => onDraftChange(candidate, { ...draft, correctedFinancialSubtype: event.target.value as FinancialSubtype | "" })}><option value="">{candidate.financial_subtype ? t(FINANCIAL_SUBTYPE_KEYS[candidate.financial_subtype]) : t("value.unspecified")}</option>{FINANCIAL_SUBTYPES.map((subtype) => <option key={subtype} value={subtype}>{t(FINANCIAL_SUBTYPE_KEYS[subtype])}</option>)}</select></label> : null}</td>
        <td>{candidate.validation_issues.length ? <ul>{candidate.validation_issues.map((issue, index) => <li key={`${candidate.extraction_id}:validation:${index}`}>{issue}</li>)}</ul> : null}{candidate.evidence_group_keys.length ? <details><summary>{t("batch.evidence_groups")} ({candidate.evidence_group_keys.length})</summary><ul>{candidate.evidence_group_keys.map((key, index) => <li key={`${candidate.extraction_id}:evidence-group:${index}`}><code>{key}</code></li>)}</ul></details> : null}{candidate.duplicate_evidence.length ? <details><summary>{t("duplicate.evidence")} ({candidate.duplicate_evidence.length})</summary><pre>{JSON.stringify(candidate.duplicate_evidence, null, 2)}</pre></details> : null}{!candidateHasException(candidate) ? <span>{t("batch.no_exception")}</span> : null}</td>
        <td><fieldset disabled={disabled}><legend>{t("batch.decision_for", { ordinal: candidate.candidate_ordinal })}</legend><label><input type="radio" name={`decision-${candidate.extraction_id}`} value="include" checked={draft.action === "include"} onChange={() => onDraftChange(candidate, { ...draft, action: "include", exclusionReason: "" })} />{t("batch.include")}</label><label><input type="radio" name={`decision-${candidate.extraction_id}`} value="exclude" checked={draft.action === "exclude"} onChange={() => onDraftChange(candidate, { ...draft, action: "exclude", correctionsText: "", correctedFinancialSubtype: "" })} />{t("batch.exclude")}</label>{draft.action === "exclude" ? <label className="compact-field"><span>{t("batch.exclusion_reason")}</span><textarea value={draft.exclusionReason} onChange={(event) => onDraftChange(candidate, { ...draft, exclusionReason: event.target.value })} /></label> : null}{persisted ? <span className="candidate-meta">{t("batch.saved_revision", { revision: persisted.decision_revision })}</span> : null}</fieldset></td>
      </tr>;
    })}</tbody>
  </table></div>;
}

export default ReviewCandidateTable;
