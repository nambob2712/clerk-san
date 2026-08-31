import { describe, expect, it } from "vitest";

import { isEditableCorrectionField } from "@/features/review/review-workspace";

describe("review correction fields", () => {
  it("limits editable fields to the server-supported correction contract", () => {
    expect(isEditableCorrectionField("counterparty", "receipt")).toBe(true);
    expect(isEditableCorrectionField("issuer_name", "recurring_bill")).toBe(true);
    expect(isEditableCorrectionField("issuer_name", "receipt")).toBe(false);
    expect(isEditableCorrectionField("line_items", "recurring_bill")).toBe(false);
  });
});
