import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ReviewCandidate } from "@/api/contracts";
import ReviewCandidateTable from "@/features/review/review-candidate-table";
import { I18nProvider, useI18n } from "@/lib/i18n";

function LocaleSwitches(): React.ReactElement {
  const { setLocale } = useI18n();
  return <><button onClick={() => setLocale("vi")}>VI</button><button onClick={() => setLocale("ja")}>JA</button></>;
}

describe("ReviewCandidateTable", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("renders repeated provider group values as distinct occurrences without duplicate React keys", () => {
    const candidate: ReviewCandidate = {
      extraction_id: "extract-1",
      batch_id: "batch-1",
      candidate_ordinal: 1,
      candidate_key: "1".repeat(64),
      record_kind: "generic_document",
      financial_subtype: null,
      source_locator: "rows/1",
      version: 1,
      status: "pending_review",
      payload: {},
      field_confidences: {},
      source_spans: {},
      validation_issues: ["repeated issue", "repeated issue"],
      evidence_group_keys: ["provider-repeat", "provider-repeat"],
      latest_decision: null,
      duplicate_evidence: [],
    };
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);

    render(<I18nProvider><ReviewCandidateTable candidates={[candidate]} drafts={{}} staleIds={new Set()} onDraftChange={() => undefined} /></I18nProvider>);

    expect(screen.getAllByText("provider-repeat", { selector: "code" })).toHaveLength(2);
    expect(screen.getAllByText("repeated issue")).toHaveLength(2);
    expect(consoleError).not.toHaveBeenCalled();
  });

  it("localizes every financial subtype option while preserving enum wire values", () => {
    const candidate: ReviewCandidate = {
      extraction_id: "extract-financial",
      batch_id: "batch-1",
      candidate_ordinal: 1,
      candidate_key: "1".repeat(64),
      record_kind: "financial",
      financial_subtype: "invoice",
      source_locator: "rows/1",
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
    const genericCandidate: ReviewCandidate = {
      ...candidate,
      extraction_id: "extract-generic",
      candidate_ordinal: 2,
      candidate_key: "2".repeat(64),
      record_kind: "generic_document",
      financial_subtype: null,
      source_locator: "rows/2",
    };

    render(<I18nProvider><LocaleSwitches /><ReviewCandidateTable candidates={[candidate, genericCandidate]} drafts={{}} staleIds={new Set()} onDraftChange={() => undefined} /></I18nProvider>);

    expect(screen.getByText("Financial record · invoice")).toBeInTheDocument();
    expect(screen.getByText("Generic document")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Receipt" })).toHaveValue("receipt");
    expect(screen.getByRole("option", { name: "Recurring bill" })).toHaveValue("recurring_bill");
    fireEvent.click(screen.getByRole("button", { name: "VI" }));
    expect(screen.getByText("Bản ghi tài chính · invoice")).toBeInTheDocument();
    expect(screen.getByText("Tài liệu chung")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Biên lai" })).toHaveValue("receipt");
    expect(screen.getByRole("option", { name: "Hóa đơn định kỳ" })).toHaveValue("recurring_bill");
    fireEvent.click(screen.getByRole("button", { name: "JA" }));
    expect(screen.getByText("財務記録 · invoice")).toBeInTheDocument();
    expect(screen.getByText("一般文書")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "領収書" })).toHaveValue("receipt");
    expect(screen.getByRole("option", { name: "定期請求" })).toHaveValue("recurring_bill");
  });
});
