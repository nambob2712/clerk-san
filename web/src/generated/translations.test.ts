import { describe, expect, it } from "vitest";

import { supportedLocales, translations } from "@/generated/translations";

describe("generated canonical translations", () => {
  it("keeps every supported locale on the English key set", () => {
    const expected = Object.keys(translations.en).sort();
    for (const locale of supportedLocales) expect(Object.keys(translations[locale]).sort()).toEqual(expected);
  });

  it("keeps all workflow vocabulary available", () => {
    expect(translations.vi["review.approve"]).toBeTruthy();
    expect(translations.ja["scan.upload"]).toBeTruthy();
    expect(translations.en["original.immutable"]).toBeTruthy();
    expect(translations.en["intake.upload_file_title"]).toBe("Upload file");
    expect(translations.vi["intake.upload_file_title"]).toBe("Tải tệp");
    expect(translations.ja["intake.scan_bill_title"]).toBe("請求書をスキャン");
    expect(translations.vi["intake.queue_summary"]).toContain("{processing}");
    expect(translations.ja["intake.processing_delayed"]).toBeTruthy();
    expect(translations.en["batch.staged_not_authoritative"]).toContain("previous active cohort");
    expect(translations.vi["mapping.validate_preview"]).toContain("xem trước");
    expect(translations.ja["original.attachment_only"]).toContain("添付ファイル");
  });

  it("preserves canonical translation values without UI-specific rewriting", () => {
    expect(translations.en["duplicate.queued"]).toContain("—");
  });
});
