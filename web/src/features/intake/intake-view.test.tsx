import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StrictMode } from "react";

import { LocalApiError } from "@/api/client";
import type { ExplicitIntakeIntent, SourceIntakeDetail } from "@/api/contracts";
import IntakeView from "@/features/intake/intake-view";
import { UploadQueue, type UploadQueueApi } from "@/features/intake/upload-queue";
import { UploadQueueProvider } from "@/features/intake/upload-queue-provider";
import { I18nProvider } from "@/lib/i18n";

function detail(intakeId: string, documentId: string, sourceId: string): SourceIntakeDetail {
  return {
    intake_id: intakeId,
    document_id: documentId,
    source_file_id: sourceId,
    source_version: 1,
    source_sha256: "a".repeat(64),
    intake_intent: "generic_file",
    state: "processed",
    reason_code: null,
    retryable: false,
    failure_phase: null,
    version: 1,
    job_reference: null,
  };
}

function renderIntake(client: UploadQueueApi, processFormats: readonly string[] = []): UploadQueue {
  let sequence = 0;
  const queue = new UploadQueue({ client, createUuid: () => `00000000-0000-4000-8000-${String(++sequence).padStart(12, "0")}` });
  render(<I18nProvider><UploadQueueProvider queue={queue}><IntakeView onReviewChanged={() => undefined} processFormats={processFormats} /></UploadQueueProvider></I18nProvider>);
  return queue;
}

describe("intent-bound intake controls", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders separate accessible generic multi-file and single bill controls", () => {
    const client: UploadQueueApi = {
      upload: vi.fn(),
      intake: vi.fn(),
      recentIntakes: vi.fn().mockResolvedValue([]),
      retryIntake: vi.fn(),
      reprocessIntake: vi.fn(),
    };
    renderIntake(client);

    const generic = screen.getByLabelText("Upload file");
    const bill = screen.getByLabelText("Scan bill");
    expect(generic).toHaveAttribute("multiple");
    expect(generic).not.toHaveAttribute("accept");
    expect(bill).not.toHaveAttribute("multiple");
    expect(bill).toHaveAttribute("accept", expect.stringContaining("application/pdf"));
    expect(screen.getByText(/For CSV, XLSX, or another document type, use Upload file/i)).toBeInTheDocument();
  });

  it("sends immutable generic intent and a different stable key for every selected file", async () => {
    let accepted = 0;
    const intake = vi.fn(async (intakeId: string) => detail(intakeId, `document-${intakeId}`, `source-${intakeId}`));
    const upload = vi.fn(async (_file: File, _intent?: ExplicitIntakeIntent, _idempotencyKey?: string) => {
      accepted += 1;
      return {
        document_id: `document-${accepted}`,
        status: "uploaded" as const,
        source_file_id: `source-${accepted}`,
        source_intake_id: `intake-${accepted}`,
      };
    });
    const client: UploadQueueApi = {
      upload,
      intake,
      recentIntakes: vi.fn().mockResolvedValue([]),
      retryIntake: vi.fn(),
      reprocessIntake: vi.fn(),
    };
    renderIntake(client);
    const csv = new File(["date,amount"], "paypay.csv", { type: "text/csv", lastModified: 1 });
    const xlsx = new File(["sheet"], "ledger.xlsx", { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", lastModified: 2 });

    fireEvent.change(screen.getByLabelText("Upload file"), { target: { files: [csv, xlsx] } });

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(2));
    expect(upload.mock.calls[0]?.[1]).toBe("generic_file");
    expect(upload.mock.calls[1]?.[1]).toBe("generic_file");
    expect(upload.mock.calls[0]?.[2]).not.toBe(upload.mock.calls[1]?.[2]);
    await waitFor(() => expect(intake).toHaveBeenCalledWith("intake-1"));
    expect(intake).toHaveBeenCalledWith("intake-2");
  });

  it("submits only one bill and labels a server rejection as not stored", async () => {
    const upload = vi.fn<UploadQueueApi["upload"]>()
      .mockRejectedValueOnce(new LocalApiError(415, { code: "intake_intent_mismatch", message: "Use Upload file for this source." }));
    const client: UploadQueueApi = {
      upload,
      intake: vi.fn(),
      recentIntakes: vi.fn().mockResolvedValue([]),
      retryIntake: vi.fn(),
      reprocessIntake: vi.fn(),
    };
    renderIntake(client);
    const first = new File(["not an image"], "transactions.csv", { type: "text/csv" });
    const second = new File(["image"], "receipt.png", { type: "image/png" });

    fireEvent.change(screen.getByLabelText("Scan bill"), { target: { files: [first, second] } });

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(1));
    expect(upload.mock.calls[0]?.[1]).toBe("bill_scan");
    expect(await screen.findByText("Rejected — not stored")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Use Upload file for this source.");
  });

  it("rehydrates a preserved CSV by canonical detected format and enables exact-source reprocess only when available", async () => {
    const stored = {
      ...detail("intake-1", "document-1", "source-1"),
      detected_format: "csv",
      state: "stored_unprocessed" as const,
      reason_code: "adapter_temporarily_unavailable",
      upload_idempotency_key: "stable-key",
    };
    const client: UploadQueueApi = {
      upload: vi.fn(),
      intake: vi.fn(),
      recentIntakes: vi.fn().mockResolvedValue([stored]),
      retryIntake: vi.fn(),
      reprocessIntake: vi.fn(),
    };
    renderIntake(client, ["csv"]);

    expect(await screen.findByRole("button", { name: "Reprocess source" })).toBeEnabled();
    const download = screen.getByRole("link", { name: "Download preserved source" });
    expect(download).toHaveAttribute("href", expect.stringContaining("source_file_id=source-1"));
    expect(download).toHaveAttribute("href", expect.stringContaining(`sha256=${"a".repeat(64)}`));
  });

  it("explains a preserved format without advertising a capability-doomed reprocess action", async () => {
    const stored = {
      ...detail("intake-1", "document-1", "source-1"),
      detected_format: "csv",
      state: "stored_unprocessed" as const,
      reason_code: "adapter_temporarily_unavailable",
    };
    const client: UploadQueueApi = {
      upload: vi.fn(),
      intake: vi.fn(),
      recentIntakes: vi.fn().mockResolvedValue([stored]),
      retryIntake: vi.fn(),
      reprocessIntake: vi.fn(),
    };
    renderIntake(client, []);

    expect(await screen.findByText(/sandbox advertises csv/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reprocess source" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download preserved source" })).toBeInTheDocument();
  });

  it("offers the server-authorized Retry action for a retryable failed intake", async () => {
    const failed = {
      ...detail("intake-1", "document-1", "source-1"),
      detected_format: "csv",
      state: "failed" as const,
      reason_code: "worker_temporarily_unavailable",
      retryable: true,
    };
    const client: UploadQueueApi = {
      upload: vi.fn(),
      intake: vi.fn(),
      recentIntakes: vi.fn().mockResolvedValue([failed]),
      retryIntake: vi.fn(),
      reprocessIntake: vi.fn(),
    };
    renderIntake(client, ["csv"]);

    expect(await screen.findByRole("button", { name: "Retry local background work" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Reprocess source" })).not.toBeInTheDocument();
  });

  it("retains a supplied app-scoped queue through React StrictMode effect replay", async () => {
    const upload = vi.fn<UploadQueueApi["upload"]>().mockResolvedValue({
      document_id: "document-1",
      status: "uploaded",
      source_file_id: "source-1",
      source_intake_id: "intake-1",
    });
    const mockClient: UploadQueueApi = {
      upload,
      intake: vi.fn().mockResolvedValue(detail("intake-1", "document-1", "source-1")),
      recentIntakes: vi.fn().mockResolvedValue([]),
      retryIntake: vi.fn(),
      reprocessIntake: vi.fn(),
    };
    const queue = new UploadQueue({ client: mockClient });
    const rendered = render(<StrictMode><I18nProvider><UploadQueueProvider queue={queue}><IntakeView onReviewChanged={() => undefined} /></UploadQueueProvider></I18nProvider></StrictMode>);

    fireEvent.change(screen.getByLabelText("Upload file"), {
      target: { files: [new File(["rows"], "paypay.csv")] },
    });

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(queue.getSnapshot()[0]?.intake?.state).toBe("processed"));
    rendered.unmount();
    queue.destroy();
  });

  it("ignores a late upload after provider unmount and resumes the reusable queue on remount", async () => {
    const accepted = {
      document_id: "document-1",
      status: "uploaded" as const,
      source_file_id: "source-1",
      source_intake_id: "intake-1",
    };
    let resolveFirstUpload!: (value: typeof accepted) => void;
    const firstUpload = new Promise<typeof accepted>((resolve) => { resolveFirstUpload = resolve; });
    const upload = vi.fn<UploadQueueApi["upload"]>()
      .mockImplementationOnce(() => firstUpload)
      .mockResolvedValueOnce(accepted);
    let persisted: string | null = null;
    const storage = {
      getItem: vi.fn(() => persisted),
      setItem: vi.fn((_key: string, value: string) => { persisted = value; }),
    };
    const mockClient: UploadQueueApi = {
      upload,
      intake: vi.fn().mockResolvedValue(detail("intake-1", "document-1", "source-1")),
      recentIntakes: vi.fn().mockResolvedValue([]),
      retryIntake: vi.fn(),
      reprocessIntake: vi.fn(),
    };
    const queue = new UploadQueue({ client: mockClient, storage });
    const renderProvider = () => render(
      <I18nProvider>
        <UploadQueueProvider queue={queue}>
          <IntakeView onReviewChanged={() => undefined} />
        </UploadQueueProvider>
      </I18nProvider>,
    );
    const firstRender = renderProvider();

    fireEvent.change(screen.getByLabelText("Upload file"), {
      target: { files: [new File(["rows"], "paypay.csv")] },
    });
    await waitFor(() => expect(queue.getSnapshot()[0]?.phase).toBe("preserving"));

    firstRender.unmount();
    const stoppedSnapshot = queue.getSnapshot();
    const stoppedPersistence = persisted;
    const stoppedWriteCount = storage.setItem.mock.calls.length;
    await act(async () => {
      resolveFirstUpload(accepted);
      await firstUpload;
      await Promise.resolve();
    });

    expect(queue.getSnapshot()).toBe(stoppedSnapshot);
    expect(persisted).toBe(stoppedPersistence);
    expect(storage.setItem).toHaveBeenCalledTimes(stoppedWriteCount);

    const secondRender = renderProvider();
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(2));
    expect(upload.mock.calls[1]?.[2]).toBe(upload.mock.calls[0]?.[2]);
    await waitFor(() => expect(queue.getSnapshot()[0]?.intake?.state).toBe("processed"));
    secondRender.unmount();
    queue.destroy();
  });
});
