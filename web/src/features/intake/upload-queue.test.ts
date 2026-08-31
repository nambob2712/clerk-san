import { waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LocalApiError } from "@/api/client";
import type { ExplicitIntakeIntent, SourceIntakeDetail, UploadAccepted } from "@/api/contracts";
import {
  MAX_POLL_CONCURRENCY,
  MAX_UPLOAD_CONCURRENCY,
  RECENT_INTAKE_LIMIT,
  UploadQueue,
  type UploadQueueApi,
} from "@/features/intake/upload-queue";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function intakeDetail(
  index: number,
  state: SourceIntakeDetail["state"] = "processed",
  intent: ExplicitIntakeIntent = "generic_file",
  version = 1,
  uploadIdempotencyKey?: string,
): SourceIntakeDetail {
  return {
    intake_id: `intake-${index}`,
    document_id: `document-${index}`,
    source_file_id: `source-${index}`,
    source_version: 1,
    source_sha256: String(index).padStart(64, "0"),
    upload_idempotency_key: uploadIdempotencyKey,
    intake_intent: intent,
    state,
    reason_code: state === "processed" ? null : "processing_queued",
    retryable: state === "failed",
    failure_phase: null,
    version,
    job_reference: null,
  };
}

function client(overrides: Partial<UploadQueueApi> = {}): UploadQueueApi {
  return {
    upload: vi.fn(),
    intake: vi.fn(),
    recentIntakes: vi.fn().mockResolvedValue([]),
    retryIntake: vi.fn(),
    reprocessIntake: vi.fn(),
    ...overrides,
  };
}

describe("UploadQueue", () => {
  const queues: UploadQueue[] = [];

  afterEach(() => {
    for (const queue of queues) queue.destroy();
    queues.length = 0;
    vi.restoreAllMocks();
  });

  it("caps uploads at two and keeps a rejected sibling independent", async () => {
    const pending = new Map([
      ["a.csv", deferred<UploadAccepted>()],
      ["b.csv", deferred<UploadAccepted>()],
      ["c.csv", deferred<UploadAccepted>()],
    ]);
    let active = 0;
    let maximum = 0;
    const upload = vi.fn(async (file: File, _intent?: ExplicitIntakeIntent, _key?: string) => {
      const own = pending.get(file.name);
      if (!own) throw new Error("missing deferred upload");
      active += 1;
      maximum = Math.max(maximum, active);
      try { return await own.promise; } finally { active -= 1; }
    });
    const mockClient = client({
      upload,
      intake: vi.fn(async (intakeId: string) => intakeDetail(Number(intakeId.split("-")[1]))),
    });
    let id = 0;
    const queue = new UploadQueue({ client: mockClient, createUuid: () => `id-${++id}` });
    queues.push(queue);
    queue.start();
    queue.enqueue([
      new File(["a"], "a.csv"),
      new File(["b"], "b.csv"),
      new File(["c"], "c.csv"),
    ], "generic_file");

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(MAX_UPLOAD_CONCURRENCY));
    expect(maximum).toBe(MAX_UPLOAD_CONCURRENCY);
    pending.get("c.csv")?.resolve({ document_id: "document-3", status: "uploaded", source_file_id: "source-3", source_intake_id: "intake-3" });
    pending.get("b.csv")?.reject(new LocalApiError(415, { code: "unsupported_file", message: "Unsupported" }));
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(3));
    pending.get("a.csv")?.resolve({ document_id: "document-1", status: "uploaded", source_file_id: "source-1", source_intake_id: "intake-1" });

    await waitFor(() => expect(queue.getSnapshot().some((item) => item.phase === "rejected")).toBe(true));
    expect(queue.getSnapshot().filter((item) => item.phase === "accepted")).toHaveLength(2);
    expect(maximum).toBe(MAX_UPLOAD_CONCURRENCY);
    expect(upload.mock.calls.map((call) => call[1])).toEqual(["generic_file", "generic_file", "generic_file"]);
    expect(new Set(upload.mock.calls.map((call) => call[2])).size).toBe(3);
  });

  it("polls at most four accepted sources and always uses each exact intake id", async () => {
    const polls = Array.from({ length: 5 }, () => deferred<SourceIntakeDetail>());
    let active = 0;
    let maximum = 0;
    const intake = vi.fn(async (intakeId: string) => {
      const index = Number(intakeId.split("-")[1]) - 1;
      active += 1;
      maximum = Math.max(maximum, active);
      try { return await polls[index].promise; } finally { active -= 1; }
    });
    let uploaded = 0;
    const upload = vi.fn(async () => {
      uploaded += 1;
      return { document_id: `document-${uploaded}`, status: "uploaded" as const, source_file_id: `source-${uploaded}`, source_intake_id: `intake-${uploaded}` };
    });
    const queue = new UploadQueue({ client: client({ upload, intake }) });
    queues.push(queue);
    queue.start();
    queue.enqueue(Array.from({ length: 5 }, (_, index) => new File([String(index)], `${index}.csv`)), "generic_file");

    await waitFor(() => expect(intake).toHaveBeenCalledTimes(MAX_POLL_CONCURRENCY));
    expect(maximum).toBe(MAX_POLL_CONCURRENCY);
    polls[0].resolve(intakeDetail(1));
    await waitFor(() => expect(intake).toHaveBeenCalledTimes(5));
    for (let index = 1; index < polls.length; index += 1) polls[index].resolve(intakeDetail(index + 1));

    await waitFor(() => expect(queue.getSnapshot().every((item) => item.intake?.state === "processed")).toBe(true));
    expect(new Set(intake.mock.calls.map((call) => call[0]))).toEqual(new Set(["intake-1", "intake-2", "intake-3", "intake-4", "intake-5"]));
    expect(maximum).toBe(MAX_POLL_CONCURRENCY);
  });

  it("fails closed after one poll when an exact endpoint returns another intake identity", async () => {
    const wrongIntake = { ...intakeDetail(2, "processing"), document_id: "document-1", source_file_id: "source-1" };
    const intake = vi.fn<UploadQueueApi["intake"]>().mockResolvedValue(wrongIntake);
    const queue = new UploadQueue({
      client: client({
        upload: vi.fn().mockResolvedValue({
          document_id: "document-1",
          status: "uploaded",
          source_file_id: "source-1",
          source_intake_id: "intake-1",
        }),
        intake,
      }),
    });
    queues.push(queue);
    queue.start();
    queue.enqueue([new File(["rows"], "paypay.csv")], "generic_file");

    await waitFor(() => expect(queue.getSnapshot()[0]).toMatchObject({
      phase: "no_longer_available",
      error: { code: "source_identity_mismatch" },
    }));
    await new Promise((resolve) => setTimeout(resolve, 25));
    expect(intake).toHaveBeenCalledTimes(1);
  });

  it("ignores a recent intake whose source checksum is not canonical SHA-256", async () => {
    const malformed = { ...intakeDetail(1), source_sha256: "not-a-digest" };
    const recentIntakes = vi.fn().mockResolvedValue([malformed]);
    const queue = new UploadQueue({
      client: client({ recentIntakes }),
    });
    queues.push(queue);
    queue.start();

    await waitFor(() => expect(recentIntakes).toHaveBeenCalledWith(RECENT_INTAKE_LIMIT));
    expect(queue.getSnapshot()).toHaveLength(0);
  });

  it("rejects unsafe or non-positive source and intake versions", async () => {
    const recentIntakes = vi.fn().mockResolvedValue([
      { ...intakeDetail(1), source_version: 1.5 },
      { ...intakeDetail(2), version: 0 },
      intakeDetail(3),
    ]);
    const queue = new UploadQueue({ client: client({ recentIntakes }) });
    queues.push(queue);
    queue.start();

    await waitFor(() => expect(queue.getSnapshot()).toHaveLength(1));
    expect(queue.getSnapshot()[0]?.accepted?.intake_id).toBe("intake-3");
  });

  it("fails closed when a newer poll changes the immutable source version or checksum", async () => {
    let clock = 0;
    const scheduled: Array<{ callback: () => void; delay: number }> = [];
    const changedSource = {
      ...intakeDetail(1, "processing", "generic_file", 2),
      source_version: 2,
      source_sha256: "f".repeat(64),
    };
    const intake = vi.fn<UploadQueueApi["intake"]>()
      .mockResolvedValueOnce(intakeDetail(1, "processing", "generic_file", 1))
      .mockResolvedValueOnce(changedSource);
    const queue = new UploadQueue({
      client: client({
        upload: vi.fn().mockResolvedValue({
          document_id: "document-1",
          status: "uploaded",
          source_file_id: "source-1",
          source_intake_id: "intake-1",
        }),
        intake,
      }),
      now: () => clock,
      setTimer: (callback, delay) => {
        scheduled.push({ callback, delay });
        return {} as ReturnType<typeof setTimeout>;
      },
      clearTimer: vi.fn(),
    });
    queues.push(queue);
    queue.start();
    queue.enqueue([new File(["rows"], "paypay.csv")], "generic_file");
    await waitFor(() => expect(queue.getSnapshot()[0]?.intake?.version).toBe(1));

    clock = 2_000;
    scheduled.at(-1)?.callback();

    await waitFor(() => expect(queue.getSnapshot()[0]).toMatchObject({
      phase: "no_longer_available",
      error: { code: "source_identity_mismatch" },
      accepted: { source_version: 1, source_sha256: "1".padStart(64, "0") },
    }));
  });

  it("backs off after an older exact response instead of polling in a tight loop", async () => {
    let clock = 0;
    const scheduled: Array<{ callback: () => void; delay: number }> = [];
    const intake = vi.fn<UploadQueueApi["intake"]>()
      .mockResolvedValueOnce(intakeDetail(1, "processing", "generic_file", 3))
      .mockResolvedValue(intakeDetail(1, "queued", "generic_file", 2));
    const queue = new UploadQueue({
      client: client({
        upload: vi.fn().mockResolvedValue({ document_id: "document-1", status: "uploaded", source_file_id: "source-1", source_intake_id: "intake-1" }),
        intake,
      }),
      now: () => clock,
      setTimer: (callback, delay) => {
        scheduled.push({ callback, delay });
        return {} as ReturnType<typeof setTimeout>;
      },
      clearTimer: vi.fn(),
    });
    queues.push(queue);
    queue.start();
    queue.enqueue([new File(["rows"], "paypay.csv")], "generic_file");
    await waitFor(() => expect(queue.getSnapshot()[0]?.intake?.version).toBe(3));
    expect(scheduled.at(-1)?.delay).toBe(2_000);

    clock = 2_000;
    scheduled.at(-1)?.callback();
    await waitFor(() => expect(intake).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(queue.getSnapshot()[0]?.poll_attempt).toBe(2));

    expect(queue.getSnapshot()[0]?.intake).toMatchObject({ state: "processing", version: 3 });
    expect(scheduled.at(-1)?.delay).toBe(4_000);
    expect(intake).toHaveBeenCalledTimes(2);
  });

  it("ignores a late exact poll after stop and polls again when restarted", async () => {
    const latePoll = deferred<SourceIntakeDetail>();
    const intake = vi.fn<UploadQueueApi["intake"]>()
      .mockImplementationOnce(() => latePoll.promise)
      .mockResolvedValueOnce(intakeDetail(1));
    const upload = vi.fn<UploadQueueApi["upload"]>().mockResolvedValue({
      document_id: "document-1",
      status: "uploaded",
      source_file_id: "source-1",
      source_intake_id: "intake-1",
    });
    let persisted: string | null = null;
    const storage = {
      getItem: vi.fn(() => persisted),
      setItem: vi.fn((_key: string, value: string) => { persisted = value; }),
    };
    const queue = new UploadQueue({ client: client({ upload, intake }), storage });
    queues.push(queue);
    queue.start();
    queue.enqueue([new File(["rows"], "paypay.csv")], "generic_file");
    await waitFor(() => expect(intake).toHaveBeenCalledTimes(1));

    queue.stop();
    const stoppedSnapshot = queue.getSnapshot();
    const stoppedPersistence = persisted;
    const stoppedWriteCount = storage.setItem.mock.calls.length;
    latePoll.resolve(intakeDetail(1));
    await latePoll.promise;
    await Promise.resolve();

    expect(queue.getSnapshot()).toBe(stoppedSnapshot);
    expect(persisted).toBe(stoppedPersistence);
    expect(storage.setItem).toHaveBeenCalledTimes(stoppedWriteCount);

    queue.start();
    await waitFor(() => expect(intake).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(queue.getSnapshot()[0]?.intake?.state).toBe("processed"));
  });

  it("rehydrates a bounded recent intake and retains its server-owned intent", async () => {
    const recent = vi.fn().mockResolvedValue([intakeDetail(7, "needs_mapping", "generic_file")]);
    const queue = new UploadQueue({ client: client({ recentIntakes: recent }) });
    queues.push(queue);
    queue.start();

    await waitFor(() => expect(queue.getSnapshot()).toHaveLength(1));
    expect(recent).toHaveBeenCalledWith(RECENT_INTAKE_LIMIT);
    expect(queue.getSnapshot()[0]).toMatchObject({
      intake_intent: "generic_file",
      phase: "accepted",
      accepted: { intake_id: "intake-7", source_file_id: "source-7" },
      intake: { state: "needs_mapping" },
    });
  });

  it("rejects rehydration metadata with a malformed upload idempotency key", async () => {
    const malformed = {
      ...intakeDetail(7, "processing", "generic_file"),
      upload_idempotency_key: 42,
    } as unknown as SourceIntakeDetail;
    const queue = new UploadQueue({
      client: client({ recentIntakes: vi.fn().mockResolvedValue([malformed, intakeDetail(8)]) }),
    });
    queues.push(queue);
    queue.start();

    await waitFor(() => expect(queue.getSnapshot()).toHaveLength(1));
    expect(queue.getSnapshot()[0]?.accepted?.intake_id).toBe("intake-8");
  });

  it("persists an in-flight item key and reuses it when the same file is reselected", async () => {
    let stored: string | null = null;
    const storage = {
      getItem: vi.fn(() => stored),
      setItem: vi.fn((_key: string, value: string) => { stored = value; }),
    };
    const interrupted = deferred<UploadAccepted>();
    let id = 0;
    const firstClient = client({ upload: vi.fn(() => interrupted.promise) });
    const firstQueue = new UploadQueue({
      client: firstClient,
      storage,
      createUuid: () => `first-${++id}`,
    });
    queues.push(firstQueue);
    firstQueue.start();
    const file = new File(["date,amount"], "paypay.csv", { type: "text/csv", lastModified: 44 });
    firstQueue.enqueue([file], "generic_file");
    await waitFor(() => expect(firstClient.upload).toHaveBeenCalledTimes(1));
    const stableKey = firstQueue.getSnapshot()[0]?.upload_idempotency_key;
    firstQueue.destroy();

    const replayUpload = vi.fn(async (_file: File, _intent?: ExplicitIntakeIntent, _idempotencyKey?: string) => ({
      document_id: "document-1",
      status: "uploaded" as const,
      source_file_id: "source-1",
      source_intake_id: "intake-1",
    }));
    const secondQueue = new UploadQueue({ client: client({ upload: replayUpload }), storage });
    queues.push(secondQueue);
    secondQueue.start();
    expect(secondQueue.getSnapshot()[0]).toMatchObject({
      phase: "upload_failed",
      upload_idempotency_key: stableKey,
    });

    secondQueue.enqueue([file], "generic_file");
    await waitFor(() => expect(replayUpload).toHaveBeenCalledTimes(1));
    expect(replayUpload.mock.calls[0]?.[2]).toBe(stableKey);
  });

  it("coalesces a lost-response item with the server intake carrying its exact upload key", async () => {
    let stored: string | null = null;
    const storage = {
      getItem: vi.fn(() => stored),
      setItem: vi.fn((_key: string, value: string) => { stored = value; }),
    };
    const interrupted = deferred<UploadAccepted>();
    let id = 0;
    const firstQueue = new UploadQueue({
      client: client({ upload: vi.fn(() => interrupted.promise) }),
      storage,
      createUuid: () => `stable-${++id}`,
    });
    queues.push(firstQueue);
    firstQueue.start();
    firstQueue.enqueue([new File(["rows"], "paypay.csv", { lastModified: 8 })], "generic_file");
    await waitFor(() => expect(firstQueue.getSnapshot()[0]?.phase).toBe("preserving"));
    const pending = firstQueue.getSnapshot()[0];
    firstQueue.destroy();

    const recent = intakeDetail(1, "processing", "generic_file", 2, pending.upload_idempotency_key);
    const secondQueue = new UploadQueue({ client: client({ recentIntakes: vi.fn().mockResolvedValue([recent]) }), storage });
    queues.push(secondQueue);
    secondQueue.start();

    await waitFor(() => expect(secondQueue.getSnapshot()[0]?.accepted?.intake_id).toBe("intake-1"));
    expect(secondQueue.getSnapshot()).toHaveLength(1);
    expect(secondQueue.getSnapshot()[0]).toMatchObject({
      client_id: pending.client_id,
      file_name: "paypay.csv",
      upload_idempotency_key: pending.upload_idempotency_key,
      phase: "accepted",
      intake: { version: 2 },
    });
  });

  it("fails closed when a persisted accepted upload key is later attached to a different intake", async () => {
    let stored: string | null = null;
    const storage = {
      getItem: vi.fn(() => stored),
      setItem: vi.fn((_key: string, value: string) => { stored = value; }),
    };
    const firstQueue = new UploadQueue({
      client: client({
        upload: vi.fn().mockResolvedValue({ document_id: "document-1", status: "uploaded", source_file_id: "source-1", source_intake_id: "intake-1" }),
        intake: vi.fn().mockResolvedValue(intakeDetail(1, "processed", "generic_file", 1, "stable-key")),
      }),
      storage,
      createUuid: vi.fn()
        .mockReturnValueOnce("client-1")
        .mockReturnValueOnce("stable-key"),
    });
    queues.push(firstQueue);
    firstQueue.start();
    firstQueue.enqueue([new File(["rows"], "paypay.csv")], "generic_file");
    await waitFor(() => expect(firstQueue.getSnapshot()[0]?.accepted?.intake_id).toBe("intake-1"));
    firstQueue.destroy();

    const conflicting = {
      ...intakeDetail(2, "processed", "generic_file", 1, "stable-key"),
      document_id: "document-2",
      source_file_id: "source-2",
    };
    const secondQueue = new UploadQueue({
      client: client({ recentIntakes: vi.fn().mockResolvedValue([conflicting]) }),
      storage,
    });
    queues.push(secondQueue);
    secondQueue.start();

    await waitFor(() => expect(secondQueue.getSnapshot()[0]?.phase).toBe("no_longer_available"));
    expect(secondQueue.getSnapshot()).toHaveLength(1);
    expect(secondQueue.getSnapshot()[0]).toMatchObject({
      accepted: { intake_id: "intake-1", document_id: "document-1", source_file_id: "source-1" },
      error: { code: "source_identity_mismatch" },
    });
  });

  it("fails closed when rehydration changes source version under the same accepted intake", async () => {
    let stored: string | null = null;
    const storage = {
      getItem: vi.fn(() => stored),
      setItem: vi.fn((_key: string, value: string) => { stored = value; }),
    };
    const stable = intakeDetail(1, "processed", "generic_file", 1, "stable-key");
    const firstQueue = new UploadQueue({
      client: client({
        upload: vi.fn().mockResolvedValue({
          document_id: "document-1",
          status: "uploaded",
          source_file_id: "source-1",
          source_intake_id: "intake-1",
        }),
        intake: vi.fn().mockResolvedValue(stable),
      }),
      storage,
      createUuid: vi.fn()
        .mockReturnValueOnce("client-1")
        .mockReturnValueOnce("stable-key"),
    });
    queues.push(firstQueue);
    firstQueue.start();
    firstQueue.enqueue([new File(["rows"], "paypay.csv")], "generic_file");
    await waitFor(() => expect(firstQueue.getSnapshot()[0]?.intake?.state).toBe("processed"));
    firstQueue.destroy();

    const changedSource = {
      ...stable,
      source_version: 2,
      source_sha256: "f".repeat(64),
      version: 2,
    };
    const secondQueue = new UploadQueue({
      client: client({ recentIntakes: vi.fn().mockResolvedValue([changedSource]) }),
      storage,
    });
    queues.push(secondQueue);
    secondQueue.start();

    await waitFor(() => expect(secondQueue.getSnapshot()[0]).toMatchObject({
      phase: "no_longer_available",
      error: { code: "source_identity_mismatch" },
      accepted: { source_version: 1, source_sha256: stable.source_sha256 },
    }));
  });

  it("fails closed on a 202 without exact source identity and safely replays with the same key", async () => {
    const upload = vi.fn<UploadQueueApi["upload"]>()
      .mockResolvedValueOnce({
        document_id: "document-1",
        status: "uploaded",
        duplicate_of: "document-duplicate",
      })
      .mockResolvedValueOnce({
        document_id: "document-1",
        status: "uploaded",
        duplicate_of: "document-duplicate",
        source_file_id: "source-1",
        source_intake_id: "intake-1",
      });
    const exact = vi.fn().mockResolvedValue(intakeDetail(1));
    const queue = new UploadQueue({ client: client({ upload, intake: exact }) });
    queues.push(queue);
    queue.start();
    queue.enqueue([new File(["rows"], "paypay.csv")], "generic_file");

    await waitFor(() => expect(queue.getSnapshot()[0]?.error?.code).toBe("accepted_identity_missing"));
    const failed = queue.getSnapshot()[0];
    expect(failed).toMatchObject({
      phase: "upload_failed",
      accepted: { document_id: "document-1", duplicate_of: "document-duplicate" },
    });
    expect(failed.file).toBeInstanceOf(File);
    expect(exact).not.toHaveBeenCalled();

    expect(queue.retryUpload(failed.client_id)).toBe(true);
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(2));
    expect(upload.mock.calls[1]?.[2]).toBe(upload.mock.calls[0]?.[2]);
    await waitFor(() => expect(exact).toHaveBeenCalledWith("intake-1"));
    expect(queue.getSnapshot()[0]?.accepted?.duplicate_of).toBe("document-duplicate");
  });

  it.each([408, 429])("retains the file and stable key for retryable HTTP %s", async (status) => {
    const upload = vi.fn<UploadQueueApi["upload"]>()
      .mockRejectedValueOnce(new LocalApiError(status, { code: "temporary_upload_failure", message: "Try again" }))
      .mockResolvedValueOnce({ document_id: "document-1", status: "uploaded", source_file_id: "source-1", source_intake_id: "intake-1" });
    const queue = new UploadQueue({ client: client({ upload, intake: vi.fn().mockResolvedValue(intakeDetail(1)) }) });
    queues.push(queue);
    queue.start();
    queue.enqueue([new File(["rows"], "paypay.csv")], "generic_file");

    await waitFor(() => expect(queue.getSnapshot()[0]?.phase).toBe("upload_failed"));
    const failed = queue.getSnapshot()[0];
    expect(failed.error?.retryable).toBe(true);
    expect(failed.file).toBeInstanceOf(File);
    queue.retryUpload(failed.client_id);
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(2));
    expect(upload.mock.calls[1]?.[2]).toBe(upload.mock.calls[0]?.[2]);
  });

  it("refreshes exact intake immediately after reprocess, merges it, then resumes bounded polling", async () => {
    let clock = 0;
    const scheduled: Array<{ callback: () => void; delay: number }> = [];
    const intake = vi.fn<UploadQueueApi["intake"]>()
      .mockResolvedValueOnce(intakeDetail(1, "failed", "generic_file", 1))
      .mockResolvedValueOnce(intakeDetail(1, "queued", "generic_file", 2))
      .mockResolvedValueOnce(intakeDetail(1, "processed", "generic_file", 3));
    const reprocess = vi.fn().mockResolvedValue({ document_id: "document-1", original_version: 1, status: "queued", job_id: "job-2" });
    const queue = new UploadQueue({
      client: client({
        upload: vi.fn().mockResolvedValue({ document_id: "document-1", status: "uploaded", source_file_id: "source-1", source_intake_id: "intake-1" }),
        intake,
        reprocessIntake: reprocess,
      }),
      now: () => clock,
      setTimer: (callback, delay) => {
        scheduled.push({ callback, delay });
        return {} as ReturnType<typeof setTimeout>;
      },
      clearTimer: vi.fn(),
    });
    queues.push(queue);
    queue.start();
    queue.enqueue([new File(["rows"], "paypay.csv")], "generic_file");
    await waitFor(() => expect(queue.getSnapshot()[0]?.intake?.state).toBe("failed"));

    await queue.reprocessIntake(queue.getSnapshot()[0].client_id);

    expect(reprocess).toHaveBeenCalledWith("intake-1", 1);
    expect(intake).toHaveBeenNthCalledWith(2, "intake-1");
    expect(queue.getSnapshot()[0]?.intake).toMatchObject({ state: "queued", version: 2 });
    expect(scheduled.at(-1)?.delay).toBe(2_000);
    clock = 2_000;
    scheduled.at(-1)?.callback();
    await waitFor(() => expect(queue.getSnapshot()[0]?.intake).toMatchObject({ state: "processed", version: 3 }));
  });

  it("keeps an accepted action pending and recovers by exact polling when its immediate refresh fails", async () => {
    const recovery = deferred<SourceIntakeDetail>();
    const intake = vi.fn<UploadQueueApi["intake"]>()
      .mockResolvedValueOnce(intakeDetail(1, "stored_unprocessed", "generic_file", 1))
      .mockRejectedValueOnce(new Error("status refresh unavailable"))
      .mockImplementationOnce(() => recovery.promise);
    const reprocess = vi.fn().mockResolvedValue({ document_id: "document-1", original_version: 1, status: "queued", job_id: "job-2" });
    const queue = new UploadQueue({
      client: client({
        upload: vi.fn().mockResolvedValue({ document_id: "document-1", status: "uploaded", source_file_id: "source-1", source_intake_id: "intake-1" }),
        intake,
        reprocessIntake: reprocess,
      }),
    });
    queues.push(queue);
    queue.start();
    queue.enqueue([new File(["rows"], "paypay.csv")], "generic_file");
    await waitFor(() => expect(queue.getSnapshot()[0]?.intake?.state).toBe("stored_unprocessed"));

    const clientId = queue.getSnapshot()[0].client_id;
    await queue.reprocessIntake(clientId);
    await waitFor(() => expect(intake).toHaveBeenCalledTimes(3));

    expect(queue.getSnapshot()[0]).toMatchObject({
      action_pending: true,
      action_refresh_pending: true,
      intake: { state: "stored_unprocessed", version: 1 },
    });
    await queue.reprocessIntake(clientId);
    expect(reprocess).toHaveBeenCalledTimes(1);

    recovery.resolve(intakeDetail(1, "queued", "generic_file", 2));
    await waitFor(() => expect(queue.getSnapshot()[0]).toMatchObject({
      action_pending: false,
      action_refresh_pending: false,
      intake: { state: "queued", version: 2 },
    }));
  });

  it("keeps an accepted action pending when its immediate refresh is older", async () => {
    let clock = 0;
    const scheduled: Array<{ callback: () => void; delay: number }> = [];
    const intake = vi.fn<UploadQueueApi["intake"]>()
      .mockResolvedValueOnce(intakeDetail(1, "stored_unprocessed", "generic_file", 3))
      .mockResolvedValueOnce(intakeDetail(1, "stored_unprocessed", "generic_file", 2))
      .mockResolvedValueOnce(intakeDetail(1, "queued", "generic_file", 4));
    const reprocess = vi.fn().mockResolvedValue({ document_id: "document-1", original_version: 1, status: "queued", job_id: "job-2" });
    const queue = new UploadQueue({
      client: client({
        upload: vi.fn().mockResolvedValue({ document_id: "document-1", status: "uploaded", source_file_id: "source-1", source_intake_id: "intake-1" }),
        intake,
        reprocessIntake: reprocess,
      }),
      now: () => clock,
      setTimer: (callback, delay) => {
        scheduled.push({ callback, delay });
        return {} as ReturnType<typeof setTimeout>;
      },
      clearTimer: vi.fn(),
    });
    queues.push(queue);
    queue.start();
    queue.enqueue([new File(["rows"], "paypay.csv")], "generic_file");
    await waitFor(() => expect(queue.getSnapshot()[0]?.intake?.version).toBe(3));

    const clientId = queue.getSnapshot()[0].client_id;
    await queue.reprocessIntake(clientId);

    expect(queue.getSnapshot()[0]).toMatchObject({
      action_pending: true,
      action_refresh_pending: true,
      intake: { state: "stored_unprocessed", version: 3 },
      error: { code: "stale_intake_response" },
    });
    await queue.reprocessIntake(clientId);
    expect(reprocess).toHaveBeenCalledTimes(1);
    expect(scheduled.at(-1)?.delay).toBe(2_000);

    clock = 2_000;
    scheduled.at(-1)?.callback();
    await waitFor(() => expect(queue.getSnapshot()[0]).toMatchObject({
      action_pending: false,
      action_refresh_pending: false,
      intake: { state: "queued", version: 4 },
    }));
  });

  it("never downgrades a stale-409 detail and immediately resumes its exact poll", async () => {
    const resumedPoll = deferred<SourceIntakeDetail>();
    const intake = vi.fn<UploadQueueApi["intake"]>()
      .mockResolvedValueOnce(intakeDetail(1, "processing", "generic_file", 3))
      .mockImplementationOnce(() => resumedPoll.promise);
    const older = intakeDetail(1, "queued", "generic_file", 2);
    const retry = vi.fn().mockRejectedValue(new LocalApiError(409, {
      code: "source_intake_stale",
      message: "Refresh",
      detail: older as never,
    }));
    const queue = new UploadQueue({
      client: client({
        upload: vi.fn().mockResolvedValue({ document_id: "document-1", status: "uploaded", source_file_id: "source-1", source_intake_id: "intake-1" }),
        intake,
        retryIntake: retry,
      }),
    });
    queues.push(queue);
    queue.start();
    queue.enqueue([new File(["rows"], "paypay.csv")], "generic_file");
    await waitFor(() => expect(queue.getSnapshot()[0]?.intake?.version).toBe(3));

    await queue.retryIntake(queue.getSnapshot()[0].client_id);

    expect(queue.getSnapshot()[0]?.intake).toMatchObject({ state: "processing", version: 3 });
    expect(queue.getSnapshot()[0]?.action_pending).toBe(false);
    expect(intake).toHaveBeenCalledTimes(2);
    resumedPoll.resolve(intakeDetail(1, "processed", "generic_file", 4));
    await waitFor(() => expect(queue.getSnapshot()[0]?.intake?.version).toBe(4));
  });

  it("rejects a stale-action detail that changes the immutable source", async () => {
    const stable = intakeDetail(1, "failed", "generic_file", 1);
    const changedSource = {
      ...intakeDetail(1, "queued", "generic_file", 2),
      source_version: 2,
      source_sha256: "f".repeat(64),
    };
    const retry = vi.fn().mockRejectedValue(new LocalApiError(409, {
      code: "source_intake_stale",
      message: "Refresh",
      detail: changedSource as never,
    }));
    const queue = new UploadQueue({
      client: client({
        upload: vi.fn().mockResolvedValue({
          document_id: "document-1",
          status: "uploaded",
          source_file_id: "source-1",
          source_intake_id: "intake-1",
        }),
        intake: vi.fn().mockResolvedValue(stable),
        retryIntake: retry,
      }),
    });
    queues.push(queue);
    queue.start();
    queue.enqueue([new File(["rows"], "paypay.csv")], "generic_file");
    await waitFor(() => expect(queue.getSnapshot()[0]?.intake?.state).toBe("failed"));

    await queue.retryIntake(queue.getSnapshot()[0].client_id);

    expect(queue.getSnapshot()[0]).toMatchObject({
      phase: "no_longer_available",
      error: { code: "source_identity_mismatch" },
      intake: { source_version: 1, source_sha256: stable.source_sha256 },
    });
  });

  it("applies a newer queued stale-409 detail and polls it immediately", async () => {
    const resumedPoll = deferred<SourceIntakeDetail>();
    const intake = vi.fn<UploadQueueApi["intake"]>()
      .mockResolvedValueOnce(intakeDetail(1, "failed", "generic_file", 1))
      .mockImplementationOnce(() => resumedPoll.promise);
    const queued = intakeDetail(1, "queued", "generic_file", 2);
    const retry = vi.fn().mockRejectedValue(new LocalApiError(409, {
      code: "source_intake_stale",
      message: "Refresh",
      detail: queued as never,
    }));
    const queue = new UploadQueue({
      client: client({
        upload: vi.fn().mockResolvedValue({ document_id: "document-1", status: "uploaded", source_file_id: "source-1", source_intake_id: "intake-1" }),
        intake,
        retryIntake: retry,
      }),
    });
    queues.push(queue);
    queue.start();
    queue.enqueue([new File(["rows"], "paypay.csv")], "generic_file");
    await waitFor(() => expect(queue.getSnapshot()[0]?.intake?.state).toBe("failed"));

    await queue.retryIntake(queue.getSnapshot()[0].client_id);

    expect(queue.getSnapshot()[0]?.intake).toMatchObject({ state: "queued", version: 2 });
    expect(intake).toHaveBeenCalledTimes(2);
    resumedPoll.resolve(intakeDetail(1, "processed", "generic_file", 3));
    await waitFor(() => expect(queue.getSnapshot()[0]?.intake?.state).toBe("processed"));
  });
});
