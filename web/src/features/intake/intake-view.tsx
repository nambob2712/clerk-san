import { useEffect, useRef } from "react";
import {
  IconFileCheck,
  IconFileUpload,
  IconReceipt2,
  IconRefresh,
  IconRoute,
  IconTrash,
} from "@tabler/icons-react";

import { api } from "@/api/client";
import { FilePolicyLegend } from "@/components/original-preview";
import { Button, Notice, PageHeading } from "@/components/ui";
import type { UploadQueueItem } from "@/features/intake/upload-queue";
import { useUploadQueue } from "@/features/intake/upload-queue-provider";
import { useI18n, type TranslationKey } from "@/lib/i18n";

const billPickerHint = "image/jpeg,image/png,image/webp,application/pdf,.jpg,.jpeg,.png,.webp,.pdf";
const SHA256_PATTERN = /^[0-9a-f]{64}$/u;

const queueErrorKeys: Partial<Record<string, TranslationKey>> = {
  accepted_identity_missing: "intake.error_identity_missing",
  accepted_status_unavailable: "intake.error_identity_missing",
  file_reselection_required: "intake.error_file_reselection",
  intake_response_intent_mismatch: "intake.error_intent_mismatch",
  invalid_source_intake_response: "intake.error_invalid_status",
  source_identity_mismatch: "intake.error_identity_mismatch",
  source_intake_not_found: "intake.error_not_found",
  source_intake_stale: "intake.error_source_stale",
  stale_intake_response: "intake.error_stale_response",
  upload_request_failed: "intake.error_upload_failed",
};

function itemStatusKey(item: UploadQueueItem): TranslationKey {
  if (item.phase === "selected") return "intake.queue_status_selected";
  if (item.phase === "preserving") return "intake.queue_status_preserving";
  if (item.phase === "rejected") return "intake.queue_status_rejected";
  if (item.phase === "upload_failed") return "intake.queue_status_upload_failed";
  if (item.phase === "no_longer_available") return "intake.queue_status_unavailable";
  if (item.action_refresh_pending) return "intake.queue_status_queued";
  if (item.intake?.state === "needs_mapping") return "intake.queue_status_needs_mapping";
  if (item.intake?.state === "stored_unprocessed") return "intake.queue_status_stored";
  if (item.intake?.state === "processed") return "intake.queue_status_processed";
  if (item.intake?.state === "failed") return "intake.queue_status_failed";
  if (item.intake?.state === "processing") return "intake.queue_status_processing";
  return "intake.queue_status_queued";
}

function QueueItem({ item, processFormats }: { item: UploadQueueItem; processFormats: readonly string[] }): React.ReactElement {
  const { t } = useI18n();
  const queue = useUploadQueue();
  const canRetryUpload = item.phase === "upload_failed" && Boolean(item.file);
  const requiredCapability = item.intake?.detected_format ?? null;
  const storedCapabilityAvailable = item.intake?.state === "stored_unprocessed"
    && requiredCapability !== null
    && processFormats.includes(requiredCapability);
  const canReprocess = item.phase === "accepted" && storedCapabilityAvailable;
  const canRetryIntake = item.phase === "accepted" && item.intake?.retryable === true && !canReprocess;
  const intentLabel = item.intake_intent === "bill_scan"
    ? t("intake.queue_intent_bill")
    : item.intake_intent === "generic_file"
      ? t("intake.queue_intent_generic")
      : t("intake.queue_intent_legacy");
  const displayName = item.file_name || t("intake.queue_preserved_source", {
    source_id: item.accepted?.source_file_id?.slice(0, 8) ?? "—",
  });
  const errorKey = item.error ? queueErrorKeys[item.error.code] : undefined;
  const errorCopy = item.error ? (errorKey ? t(errorKey) : item.error.message) : null;

  return <article className="processing-card" data-client-id={item.client_id}>
    <div>
      <span className="eyebrow">{intentLabel}</span>
      <h2>{displayName}</h2>
      <p>{t(itemStatusKey(item))}</p>
      {item.accepted?.intake_id ? <p className="muted-copy">{t("intake.queue_intake_id", { intake_id: item.accepted.intake_id })}</p> : null}
      {item.accepted?.duplicate_of ? <p>{t("intake.queue_duplicate", { document_id: item.accepted.duplicate_of })}</p> : null}
      {item.phase === "rejected" ? <p role="alert">{errorCopy ?? t("intake.queue_rejected_default")}</p> : null}
      {item.phase !== "rejected" && errorCopy ? <p role="alert">{errorCopy}</p> : null}
      {item.intake?.state === "needs_mapping" && item.intake_intent === "generic_file" ? <p>{t("intake.queue_mapping_copy")}</p> : null}
      {item.intake?.state === "stored_unprocessed" && item.intake.reason_code ? <p>{t("intake.queue_reason", { reason: item.intake.reason_code })}</p> : null}
      {item.intake?.state === "stored_unprocessed" && !storedCapabilityAvailable ? <p>{t("intake.queue_reprocess_waiting", { format: requiredCapability ?? t("value.unspecified") })}</p> : null}
    </div>
    <div className="processing-actions">
      {item.phase === "selected" ? <Button className="button-quiet" onClick={() => queue.cancel(item.client_id)}><IconTrash size={18} />{t("intake.queue_cancel")}</Button> : null}
      {canRetryUpload ? <Button className="button-secondary" onClick={() => queue.retryUpload(item.client_id)}><IconRefresh size={18} />{t("intake.queue_retry_upload")}</Button> : null}
      {canRetryIntake ? <Button className="button-secondary" onClick={() => void queue.retryIntake(item.client_id)} disabled={item.action_pending}><IconRefresh size={18} />{t("scan.retry_background")}</Button> : null}
      {canReprocess ? <Button className="button-primary" onClick={() => void queue.reprocessIntake(item.client_id)} disabled={item.action_pending}><IconRoute size={18} />{t("scan.reprocess")}</Button> : null}
      {item.intake?.state === "stored_unprocessed" && item.accepted?.document_id && item.intake.source_file_id && SHA256_PATTERN.test(item.intake.source_sha256) ? <a className="button button-secondary" href={api.originalPath(item.accepted.document_id, item.intake.source_version, item.intake.source_file_id, item.intake.source_sha256)}><IconFileCheck size={18} />{t("intake.download_preserved")}</a> : null}
      {item.intake?.state === "needs_mapping" && item.intake_intent === "generic_file" && item.accepted?.document_id ? <a className="button button-primary" href={`#mapping/${encodeURIComponent(item.accepted.document_id)}`}><IconRoute size={18} />{t("intake.open_mapping")}</a> : null}
      {item.intake?.state === "processed" ? <><p className="ready-copy"><IconFileCheck size={18} />{t("intake.ready_for_review")}</p><a className="button button-secondary" href="#review">{t("intake.open_review")}</a></> : null}
    </div>
  </article>;
}

export function IntakeView({
  onReviewChanged,
  intakeEnabled = true,
  processingDelayed = false,
  universalFormatsAvailable = true,
  processFormats = [],
}: {
  onReviewChanged: () => void;
  intakeEnabled?: boolean;
  processingDelayed?: boolean;
  universalFormatsAvailable?: boolean;
  processFormats?: readonly string[];
}): React.ReactElement {
  const { t } = useI18n();
  const queue = useUploadQueue();
  const genericInputRef = useRef<HTMLInputElement>(null);
  const billInputRef = useRef<HTMLInputElement>(null);
  const announcedProcessed = useRef(new Set<string>());

  useEffect(() => {
    const newlyProcessed = queue.items.filter((item) =>
      item.intake?.state === "processed" && !announcedProcessed.current.has(item.client_id),
    );
    if (newlyProcessed.length === 0) return;
    for (const item of newlyProcessed) announcedProcessed.current.add(item.client_id);
    onReviewChanged();
  }, [onReviewChanged, queue.items]);

  const selectGenericFiles = (files: FileList | null): void => {
    if (!files || !intakeEnabled) return;
    queue.enqueue(files, "generic_file");
  };
  const selectBill = (files: FileList | null): void => {
    if (!files || !intakeEnabled) return;
    queue.enqueue(files, "bill_scan");
  };

  return <div className="page-stack">
    <PageHeading title={t("page.add_documents.title")} copy={t("page.add_documents.copy")} />
    {!intakeEnabled ? <Notice tone="warning">{t("intake.storage_blocked")}</Notice> : null}
    {intakeEnabled && processingDelayed ? <Notice tone="info">{t("intake.processing_delayed")}</Notice> : null}
    {intakeEnabled && !universalFormatsAvailable ? <Notice tone="warning">{t("intake.universal_unavailable")}</Notice> : null}
    <section className="intake-grid" aria-label={t("intake.choose_path")}>
      <div className="intake-dropzone">
        <IconFileUpload size={42} stroke={1.35} aria-hidden="true" />
        <h2>{t("intake.upload_file_title")}</h2>
        <p>{t(universalFormatsAvailable ? "intake.upload_file_copy" : "intake.upload_file_legacy_copy")}</p>
        <input
          ref={genericInputRef}
          type="file"
          multiple
          tabIndex={-1}
          aria-label={t("intake.upload_file_title")}
          disabled={!intakeEnabled}
          onChange={(event) => {
            selectGenericFiles(event.currentTarget.files);
            event.currentTarget.value = "";
          }}
        />
        <Button className="button-primary" onClick={() => genericInputRef.current?.click()} disabled={!intakeEnabled}>
          <IconFileUpload size={18} />{t("intake.upload_file_title")}
        </Button>
      </div>
      <div className="intake-dropzone">
        <IconReceipt2 size={42} stroke={1.35} aria-hidden="true" />
        <h2>{t("intake.scan_bill_title")}</h2>
        <p>{t("intake.scan_bill_copy")}</p>
        <input
          ref={billInputRef}
          type="file"
          accept={billPickerHint}
          tabIndex={-1}
          aria-label={t("intake.scan_bill_title")}
          disabled={!intakeEnabled}
          onChange={(event) => {
            selectBill(event.currentTarget.files);
            event.currentTarget.value = "";
          }}
        />
        <Button className="button-primary" onClick={() => billInputRef.current?.click()} disabled={!intakeEnabled}>
          <IconReceipt2 size={18} />{t("intake.scan_bill_title")}
        </Button>
      </div>
    </section>
    <aside className="intake-policy"><span className="eyebrow">{t("intake.local_boundary")}</span><h2>{t("intake.one_route")}</h2><p>{t("intake.boundary_copy")}</p><FilePolicyLegend /></aside>
    <p className="muted-copy" aria-live="polite" aria-atomic="true">
      {queue.items.length === 0
        ? t("intake.queue_empty")
        : t("intake.queue_summary", queue.summary)}
    </p>
    {queue.items.length > 0 ? <section aria-label={t("intake.queue_label")} className="page-stack">
      {queue.items.map((item) => <QueueItem key={item.client_id} item={item} processFormats={processFormats} />)}
    </section> : null}
  </div>;
}

export default IntakeView;
