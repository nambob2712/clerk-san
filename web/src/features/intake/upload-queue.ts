import { LocalApiError, api } from "@/api/client";
import type {
  ExplicitIntakeIntent,
  JobAccepted,
  SourceIntakeDetail,
  UploadAccepted,
} from "@/api/contracts";

export const MAX_UPLOAD_CONCURRENCY = 2;
export const MAX_POLL_CONCURRENCY = 4;
export const RECENT_INTAKE_LIMIT = 50;

const STORAGE_KEY = "clerksan.upload-queue.v1";
const TERMINAL_STATES = new Set(["processed", "needs_mapping", "stored_unprocessed", "failed"]);
const BASE_POLL_DELAY_MS = 2_000;
const MAX_POLL_DELAY_MS = 30_000;

export type UploadSubmissionPhase =
  | "selected"
  | "preserving"
  | "accepted"
  | "rejected"
  | "upload_failed"
  | "no_longer_available";

export interface UploadQueueError {
  code: string;
  message: string;
  retryable: boolean;
}

export interface AcceptedSourceIdentity {
  document_id: string;
  intake_id?: string;
  source_file_id?: string;
  source_version?: number;
  source_sha256?: string;
  duplicate_of?: string | null;
}

export interface UploadQueueItem {
  readonly client_id: string;
  readonly intake_intent: ExplicitIntakeIntent | "legacy_unspecified";
  readonly upload_idempotency_key: string;
  file_name: string;
  file_size: number;
  file_last_modified: number;
  file?: File;
  phase: UploadSubmissionPhase;
  accepted?: AcceptedSourceIdentity;
  intake?: SourceIntakeDetail;
  error?: UploadQueueError;
  poll_attempt: number;
  next_poll_at_ms?: number;
  action_pending: boolean;
  action_refresh_pending?: boolean;
  created_at_ms: number;
}

export interface UploadQueueApi {
  upload: (
    file: File,
    intent?: ExplicitIntakeIntent,
    idempotencyKey?: string,
  ) => Promise<UploadAccepted>;
  intake: (intakeId: string) => Promise<SourceIntakeDetail>;
  recentIntakes: (limit?: number) => Promise<SourceIntakeDetail[]>;
  retryIntake: (intakeId: string, expectedVersion: number, actor?: string) => Promise<JobAccepted>;
  reprocessIntake: (intakeId: string, expectedVersion: number, actor?: string) => Promise<JobAccepted>;
}

interface PersistedQueueItem {
  client_id: string;
  intake_intent: UploadQueueItem["intake_intent"];
  upload_idempotency_key: string;
  file_name: string;
  file_size: number;
  file_last_modified: number;
  phase: "accepted" | "upload_failed";
  accepted?: AcceptedSourceIdentity;
  intake?: SourceIntakeDetail;
  created_at_ms: number;
}

interface QueueStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

interface UploadQueueOptions {
  client?: UploadQueueApi;
  storage?: QueueStorage | null;
  createUuid?: () => string;
  now?: () => number;
  setTimer?: (callback: () => void, delayMs: number) => ReturnType<typeof setTimeout>;
  clearTimer?: (timer: ReturnType<typeof setTimeout>) => void;
}

type Listener = () => void;

function defaultUuid(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") globalThis.crypto.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function isExplicitIntent(value: unknown): value is ExplicitIntakeIntent {
  return value === "generic_file" || value === "bill_scan";
}

function isIntakeIntent(value: unknown): value is UploadQueueItem["intake_intent"] {
  return value === "legacy_unspecified" || isExplicitIntent(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/u.test(value);
}

function isPositiveSafeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 1;
}

function isSourceIntakeDetail(value: unknown): value is SourceIntakeDetail {
  if (typeof value !== "object" || value === null) return false;
  const detail = value as Partial<SourceIntakeDetail>;
  return isNonEmptyString(detail.intake_id)
    && isNonEmptyString(detail.document_id)
    && isNonEmptyString(detail.source_file_id)
    && isPositiveSafeInteger(detail.source_version)
    && isSha256(detail.source_sha256)
    && (detail.upload_idempotency_key === undefined || detail.upload_idempotency_key === null || isNonEmptyString(detail.upload_idempotency_key))
    && isIntakeIntent(detail.intake_intent)
    && typeof detail.state === "string"
    && ["queued", "processing", "processed", "needs_mapping", "stored_unprocessed", "failed"].includes(detail.state)
    && typeof detail.retryable === "boolean"
    && isPositiveSafeInteger(detail.version);
}

function isAcceptedSourceIdentity(value: unknown): value is AcceptedSourceIdentity {
  if (typeof value !== "object" || value === null) return false;
  const identity = value as Partial<AcceptedSourceIdentity>;
  const hasBoundSource = identity.source_version !== undefined || identity.source_sha256 !== undefined;
  return isNonEmptyString(identity.document_id)
    && (identity.intake_id === undefined || isNonEmptyString(identity.intake_id))
    && (identity.source_file_id === undefined || isNonEmptyString(identity.source_file_id))
    && (!hasBoundSource || (isPositiveSafeInteger(identity.source_version) && isSha256(identity.source_sha256)))
    && (identity.duplicate_of === undefined || identity.duplicate_of === null || isNonEmptyString(identity.duplicate_of));
}

function sourceIdentityChanged(item: Pick<UploadQueueItem, "accepted" | "intake">, detail: SourceIntakeDetail): boolean {
  const accepted = item.accepted;
  const establishedVersion = accepted?.source_version ?? item.intake?.source_version;
  const establishedSha256 = accepted?.source_sha256 ?? item.intake?.source_sha256;
  return (accepted?.intake_id !== undefined && accepted.intake_id !== detail.intake_id)
    || (accepted?.document_id !== undefined && accepted.document_id !== detail.document_id)
    || (accepted?.source_file_id !== undefined && accepted.source_file_id !== detail.source_file_id)
    || (establishedVersion !== undefined && establishedVersion !== detail.source_version)
    || (establishedSha256 !== undefined && establishedSha256 !== detail.source_sha256);
}

function bindAcceptedSource(
  accepted: AcceptedSourceIdentity | undefined,
  detail: SourceIntakeDetail,
): AcceptedSourceIdentity {
  return {
    document_id: detail.document_id,
    intake_id: detail.intake_id,
    source_file_id: detail.source_file_id,
    source_version: detail.source_version,
    source_sha256: detail.source_sha256,
    duplicate_of: accepted?.duplicate_of ?? null,
  };
}

function isTerminal(detail: SourceIntakeDetail | undefined): boolean {
  return detail !== undefined && TERMINAL_STATES.has(detail.state);
}

function isAcceptedUpload(value: unknown): value is UploadAccepted {
  if (typeof value !== "object" || value === null) return false;
  const outcome = value as Partial<UploadAccepted>;
  return outcome.status === "uploaded" && isNonEmptyString(outcome.document_id);
}

function uploadError(reason: unknown): UploadQueueError {
  if (reason instanceof LocalApiError) {
    return {
      code: reason.code,
      message: reason.message,
      retryable: reason.status >= 500 || reason.status === 408 || reason.status === 429,
    };
  }
  return {
    code: "upload_request_failed",
    message: reason instanceof Error ? reason.message : "The upload request failed.",
    retryable: true,
  };
}

function signature(item: Pick<UploadQueueItem, "intake_intent" | "file_name" | "file_size" | "file_last_modified">): string {
  return `${item.intake_intent}\u0000${item.file_name}\u0000${item.file_size}\u0000${item.file_last_modified}`;
}

function readPersisted(storage: QueueStorage | null): PersistedQueueItem[] {
  if (!storage) return [];
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap((candidate): PersistedQueueItem[] => {
      if (typeof candidate !== "object" || candidate === null) return [];
      const item = candidate as Partial<PersistedQueueItem>;
      if (
        typeof item.client_id !== "string"
        || !isIntakeIntent(item.intake_intent)
        || typeof item.upload_idempotency_key !== "string"
        || typeof item.file_name !== "string"
        || typeof item.file_size !== "number"
        || typeof item.file_last_modified !== "number"
        || (item.phase !== "accepted" && item.phase !== "upload_failed")
        || typeof item.created_at_ms !== "number"
      ) return [];
      if (item.accepted !== undefined && !isAcceptedSourceIdentity(item.accepted)) return [];
      if (item.phase === "accepted" && (!item.accepted?.intake_id || !item.accepted.source_file_id)) return [];
      if (item.intake && isSourceIntakeDetail(item.intake) && sourceIdentityChanged(item as UploadQueueItem, item.intake)) return [];
      return [{
        ...item,
        intake: item.intake && isSourceIntakeDetail(item.intake) ? item.intake : undefined,
      } as PersistedQueueItem];
    }).slice(0, RECENT_INTAKE_LIMIT);
  } catch {
    return [];
  }
}

export class UploadQueue {
  private readonly client: UploadQueueApi;
  private readonly storage: QueueStorage | null;
  private readonly createUuid: () => string;
  private readonly now: () => number;
  private readonly setTimer: UploadQueueOptions["setTimer"];
  private readonly clearTimer: UploadQueueOptions["clearTimer"];
  private readonly records = new Map<string, UploadQueueItem>();
  private readonly order: string[] = [];
  private readonly listeners = new Set<Listener>();
  private readonly uploadRuns = new Map<string, number>();
  private readonly polling = new Set<string>();
  private snapshot: readonly UploadQueueItem[] = [];
  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private pollTimerToken = 0;
  private activeUploads = 0;
  private activePolls = 0;
  private lifecycleGeneration = 0;
  private started = false;
  private destroyed = false;
  private pollingPaused = false;

  constructor(options: UploadQueueOptions = {}) {
    this.client = options.client ?? api;
    this.storage = options.storage ?? null;
    this.createUuid = options.createUuid ?? defaultUuid;
    this.now = options.now ?? Date.now;
    this.setTimer = options.setTimer ?? ((callback, delayMs) => setTimeout(callback, delayMs));
    this.clearTimer = options.clearTimer ?? clearTimeout;
    for (const persisted of readPersisted(this.storage)) {
      const restored: UploadQueueItem = {
        ...persisted,
        phase: persisted.phase,
        poll_attempt: 0,
        action_pending: false,
        error: persisted.phase === "upload_failed"
          ? { code: "file_reselection_required", message: "Select this file again to retry with the same upload key.", retryable: true }
          : undefined,
      };
      this.records.set(restored.client_id, restored);
      this.order.push(restored.client_id);
    }
    this.refreshSnapshot(false);
  }

  getSnapshot = (): readonly UploadQueueItem[] => this.snapshot;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  start(): void {
    if (this.started || this.destroyed) return;
    this.started = true;
    this.lifecycleGeneration += 1;
    this.resumeInterruptedUploads();
    const generation = this.lifecycleGeneration;
    void this.rehydrate(generation);
    this.pumpUploads();
    this.pumpPolls();
  }

  stop(): void {
    if (!this.started) return;
    this.started = false;
    this.lifecycleGeneration += 1;
    this.clearPollTimer();
  }

  destroy(): void {
    this.stop();
    this.destroyed = true;
    this.listeners.clear();
  }

  setPollingPaused(paused: boolean): void {
    this.pollingPaused = paused;
    if (paused) this.clearPollTimer();
    if (!paused) this.pumpPolls();
  }

  enqueue(files: Iterable<File>, intakeIntent: ExplicitIntakeIntent): string[] {
    const selectedFiles = Array.from(files);
    const boundedFiles = intakeIntent === "bill_scan" ? selectedFiles.slice(0, 1) : selectedFiles;
    const clientIds: string[] = [];
    for (const file of boundedFiles) {
      const fileSignature = signature({
        intake_intent: intakeIntent,
        file_name: file.name,
        file_size: file.size,
        file_last_modified: file.lastModified,
      });
      const resumable = this.snapshot.find((item) =>
        item.phase === "upload_failed" && !item.file && signature(item) === fileSignature,
      );
      if (resumable) {
        this.replace(resumable.client_id, {
          ...resumable,
          file,
          phase: "selected",
          error: undefined,
        });
        clientIds.push(resumable.client_id);
        continue;
      }
      const item: UploadQueueItem = {
        client_id: this.createUuid(),
        intake_intent: intakeIntent,
        upload_idempotency_key: this.createUuid(),
        file_name: file.name,
        file_size: file.size,
        file_last_modified: file.lastModified,
        file,
        phase: "selected",
        poll_attempt: 0,
        action_pending: false,
        created_at_ms: this.now(),
      };
      this.records.set(item.client_id, item);
      this.order.unshift(item.client_id);
      clientIds.push(item.client_id);
    }
    this.refreshSnapshot();
    Promise.resolve().then(() => this.pumpUploads());
    return clientIds;
  }

  cancel(clientId: string): boolean {
    const item = this.records.get(clientId);
    if (!item || item.phase !== "selected") return false;
    this.records.delete(clientId);
    const index = this.order.indexOf(clientId);
    if (index >= 0) this.order.splice(index, 1);
    this.refreshSnapshot();
    return true;
  }

  retryUpload(clientId: string): boolean {
    const item = this.records.get(clientId);
    if (!item || item.phase !== "upload_failed" || !item.file) return false;
    this.replace(clientId, { ...item, phase: "selected", error: undefined });
    Promise.resolve().then(() => this.pumpUploads());
    return true;
  }

  async retryIntake(clientId: string): Promise<void> {
    await this.runIntakeAction(clientId, "retry");
  }

  async reprocessIntake(clientId: string): Promise<void> {
    await this.runIntakeAction(clientId, "reprocess");
  }

  private async rehydrate(generation: number): Promise<void> {
    try {
      const details = await this.client.recentIntakes(RECENT_INTAKE_LIMIT);
      if (!this.isLifecycleActive(generation)) return;
      for (const detail of details) this.mergeRehydrated(detail);
      this.refreshSnapshot();
      this.pumpPolls();
    } catch {
      // A listing outage must not prevent new independent uploads. Existing accepted
      // metadata remains available and exact polling can recover it later.
      if (this.isLifecycleActive(generation)) this.pumpPolls();
    }
  }

  private mergeRehydrated(detail: SourceIntakeDetail): void {
    if (!isSourceIntakeDetail(detail)) return;
    const byIntake = this.snapshot.find((item) => item.accepted?.intake_id === detail.intake_id);
    const byUploadKey = detail.upload_idempotency_key
      ? this.snapshot.find((item) => item.upload_idempotency_key === detail.upload_idempotency_key)
      : undefined;
    if (byUploadKey?.accepted?.intake_id && byUploadKey.accepted.intake_id !== detail.intake_id) {
      this.replace(byUploadKey.client_id, {
        ...byUploadKey,
        phase: "no_longer_available",
        error: { code: "source_identity_mismatch", message: "The upload key resolved to a different immutable intake.", retryable: false },
      }, false);
      return;
    }
    const existing = byUploadKey ?? byIntake;
    if (byIntake && byUploadKey && byIntake.client_id !== byUploadKey.client_id) {
      this.records.delete(byIntake.client_id);
      const duplicateIndex = this.order.indexOf(byIntake.client_id);
      if (duplicateIndex >= 0) this.order.splice(duplicateIndex, 1);
    }
    if (existing) {
      if (!existing.intake || detail.version >= existing.intake.version) {
        const keyChanged = detail.upload_idempotency_key !== null
          && detail.upload_idempotency_key !== undefined
          && existing.upload_idempotency_key !== detail.upload_idempotency_key;
        const identityChanged = sourceIdentityChanged(existing, detail);
        if (existing.intake_intent !== detail.intake_intent || keyChanged || identityChanged) {
          this.replace(existing.client_id, {
            ...existing,
            phase: "no_longer_available",
            error: { code: "source_identity_mismatch", message: "The intake response changed immutable upload identity.", retryable: false },
          }, false);
          return;
        }
        this.replace(existing.client_id, {
          ...existing,
          file: undefined,
          phase: "accepted",
          accepted: bindAcceptedSource(existing.accepted, detail),
          intake: detail,
          error: undefined,
          poll_attempt: 0,
          next_poll_at_ms: isTerminal(detail) ? undefined : this.now() + BASE_POLL_DELAY_MS,
        }, false);
      }
      return;
    }
    const item: UploadQueueItem = {
      client_id: `rehydrated:${detail.intake_id}`,
      intake_intent: detail.intake_intent,
      upload_idempotency_key: detail.upload_idempotency_key ?? this.createUuid(),
      file_name: "",
      file_size: 0,
      file_last_modified: 0,
      phase: "accepted",
      accepted: bindAcceptedSource(undefined, detail),
      intake: detail,
      poll_attempt: 0,
      action_pending: false,
      created_at_ms: this.now(),
      next_poll_at_ms: isTerminal(detail) ? undefined : this.now() + BASE_POLL_DELAY_MS,
    };
    this.records.set(item.client_id, item);
    this.order.push(item.client_id);
  }

  private pumpUploads(): void {
    if (this.destroyed || !this.started) return;
    while (this.activeUploads < MAX_UPLOAD_CONCURRENCY) {
      const next = this.snapshot.find((item) =>
        item.phase === "selected" && item.file && !this.uploadRuns.has(item.client_id),
      );
      if (!next) break;
      const generation = this.lifecycleGeneration;
      this.activeUploads += 1;
      this.uploadRuns.set(next.client_id, generation);
      this.replace(next.client_id, { ...next, phase: "preserving", error: undefined });
      void this.uploadOne(next.client_id, generation);
    }
  }

  private async uploadOne(clientId: string, generation: number): Promise<void> {
    const item = this.records.get(clientId);
    if (!item?.file || !isExplicitIntent(item.intake_intent)) {
      this.finishUpload(clientId, generation);
      return;
    }
    try {
      const outcome = await this.client.upload(item.file, item.intake_intent, item.upload_idempotency_key);
      if (!this.isLifecycleActive(generation)) return;
      if (!isAcceptedUpload(outcome)) throw new Error("Local API returned an invalid upload acceptance response.");
      const accepted: AcceptedSourceIdentity = {
        document_id: outcome.document_id,
        intake_id: outcome.source_intake_id ?? undefined,
        source_file_id: outcome.source_file_id ?? undefined,
        duplicate_of: outcome.duplicate_of ?? null,
      };
      if (!isNonEmptyString(accepted.intake_id) || !isNonEmptyString(accepted.source_file_id)) {
        this.replace(clientId, {
          ...item,
          phase: "upload_failed",
          accepted,
          error: {
            code: "accepted_identity_missing",
            message: "The source may be preserved, but the response omitted its exact intake identity. Retry with the same key.",
            retryable: true,
          },
        });
        return;
      }
      this.replace(clientId, {
        ...item,
        file: undefined,
        phase: "accepted",
        accepted,
        error: undefined,
        poll_attempt: 0,
        next_poll_at_ms: this.now(),
      });
      this.pumpPolls();
    } catch (reason) {
      if (!this.isLifecycleActive(generation)) return;
      const error = uploadError(reason);
      const rejected = reason instanceof LocalApiError
        && reason.status >= 400
        && reason.status < 500
        && reason.status !== 408
        && reason.status !== 429;
      this.replace(clientId, {
        ...item,
        file: rejected ? undefined : item.file,
        phase: rejected ? "rejected" : "upload_failed",
        error,
      });
    } finally {
      this.finishUpload(clientId, generation);
    }
  }

  private pumpPolls(): void {
    if (this.destroyed || !this.started || this.pollingPaused) return;
    this.clearPollTimer();
    const now = this.now();
    const candidates = this.snapshot.filter((item) =>
      item.phase === "accepted"
      && item.accepted?.intake_id
      && (!isTerminal(item.intake) || item.action_refresh_pending === true)
      && (!item.action_pending || item.action_refresh_pending === true)
      && !this.polling.has(item.client_id),
    );
    const due = candidates.filter((item) => this.nextPollAt(item) <= now);
    for (const item of due.slice(0, Math.max(0, MAX_POLL_CONCURRENCY - this.activePolls))) {
      const generation = this.lifecycleGeneration;
      this.polling.add(item.client_id);
      this.activePolls += 1;
      void this.pollOne(item.client_id, generation);
    }
    const future = candidates.filter((item) => !this.polling.has(item.client_id));
    if (future.length > 0 && this.activePolls < MAX_POLL_CONCURRENCY) {
      const nextAt = Math.min(...future.map((item) => this.nextPollAt(item)));
      const delay = Math.max(0, nextAt - this.now());
      const generation = this.lifecycleGeneration;
      const timerToken = ++this.pollTimerToken;
      this.pollTimer = this.setTimer?.(() => {
        if (timerToken !== this.pollTimerToken || !this.isLifecycleActive(generation)) return;
        this.pollTimer = null;
        this.pumpPolls();
      }, delay) ?? null;
    }
  }

  private nextPollAt(item: UploadQueueItem): number {
    return item.next_poll_at_ms ?? this.now();
  }

  private async pollOne(clientId: string, generation: number): Promise<void> {
    const before = this.records.get(clientId);
    const intakeId = before?.accepted?.intake_id;
    if (!before || !intakeId) return;
    try {
      const detail = await this.client.intake(intakeId);
      if (!this.isLifecycleActive(generation)) return;
      const current = this.records.get(clientId);
      if (!current) return;
      if (!isSourceIntakeDetail(detail)) {
        this.replace(clientId, {
          ...current,
          phase: "no_longer_available",
          action_pending: false,
          action_refresh_pending: false,
          error: { code: "invalid_source_intake_response", message: "The local API returned invalid intake status.", retryable: false },
        });
        return;
      }
      if (current.accepted?.intake_id !== detail.intake_id) {
        this.replace(clientId, {
          ...current,
          error: { code: "source_identity_mismatch", message: "The intake response did not match the requested immutable intake.", retryable: false },
          phase: "no_longer_available",
          action_pending: false,
          action_refresh_pending: false,
        });
        return;
      }
      if (sourceIdentityChanged(current, detail)) {
        this.replace(clientId, {
          ...current,
          error: { code: "source_identity_mismatch", message: "The intake response did not match the preserved source.", retryable: false },
          phase: "no_longer_available",
          action_pending: false,
          action_refresh_pending: false,
        });
        return;
      }
      if (detail.intake_intent !== current.intake_intent) {
        this.replace(clientId, {
          ...current,
          error: { code: "intake_response_intent_mismatch", message: "The intake response changed the selected upload path.", retryable: false },
          phase: "no_longer_available",
          action_pending: false,
          action_refresh_pending: false,
        });
        return;
      }
      const delay = Math.min(BASE_POLL_DELAY_MS * (2 ** Math.min(current.poll_attempt, 4)), MAX_POLL_DELAY_MS);
      if (current.intake && detail.version < current.intake.version) {
        this.replace(clientId, {
          ...current,
          poll_attempt: current.poll_attempt + 1,
          next_poll_at_ms: this.now() + delay,
        });
        return;
      }
      this.replace(clientId, {
        ...current,
        accepted: bindAcceptedSource(current.accepted, detail),
        intake: detail,
        action_pending: false,
        action_refresh_pending: false,
        error: undefined,
        poll_attempt: current.poll_attempt + 1,
        next_poll_at_ms: isTerminal(detail) ? undefined : this.now() + delay,
      });
    } catch (reason) {
      if (!this.isLifecycleActive(generation)) return;
      const current = this.records.get(clientId);
      if (!current) return;
      if (reason instanceof LocalApiError && reason.status === 404) {
        this.replace(clientId, {
          ...current,
          phase: "no_longer_available",
          action_pending: false,
          action_refresh_pending: false,
          error: { code: reason.code, message: reason.message, retryable: false },
        });
      } else {
        this.replace(clientId, {
          ...current,
          error: uploadError(reason),
          poll_attempt: current.poll_attempt + 1,
          next_poll_at_ms: this.now() + Math.min(BASE_POLL_DELAY_MS * (2 ** Math.min(current.poll_attempt, 4)), MAX_POLL_DELAY_MS),
        });
      }
    } finally {
      this.polling.delete(clientId);
      this.activePolls -= 1;
      if (!this.destroyed && this.started) this.pumpPolls();
    }
  }

  private isLifecycleActive(generation: number): boolean {
    return !this.destroyed && this.started && generation === this.lifecycleGeneration;
  }

  private clearPollTimer(): void {
    this.pollTimerToken += 1;
    if (this.pollTimer !== null) this.clearTimer?.(this.pollTimer);
    this.pollTimer = null;
  }

  private resumeInterruptedUploads(): void {
    let changed = false;
    for (const item of this.snapshot) {
      if (item.phase !== "preserving" || !item.file || this.uploadRuns.has(item.client_id)) continue;
      this.records.set(item.client_id, { ...item, phase: "selected" });
      changed = true;
    }
    if (changed) this.refreshSnapshot();
  }

  private finishUpload(clientId: string, generation: number): void {
    if (this.uploadRuns.get(clientId) !== generation) return;
    this.uploadRuns.delete(clientId);
    this.activeUploads -= 1;
    if (this.destroyed || !this.started) return;
    const current = this.records.get(clientId);
    if (!this.isLifecycleActive(generation) && current?.phase === "preserving" && current.file) {
      this.replace(clientId, { ...current, phase: "selected" });
    }
    this.pumpUploads();
  }

  private async runIntakeAction(clientId: string, action: "retry" | "reprocess"): Promise<void> {
    const item = this.records.get(clientId);
    const intake = item?.intake;
    if (!item || !intake || item.action_pending) return;
    this.replace(clientId, { ...item, action_pending: true, error: undefined });
    try {
      if (action === "retry") await this.client.retryIntake(intake.intake_id, intake.version);
      else await this.client.reprocessIntake(intake.intake_id, intake.version);
    } catch (reason) {
      if (this.destroyed) return;
      const current = this.records.get(clientId);
      if (!current) return;
      if (reason instanceof LocalApiError && reason.isStaleSourceIntake && isSourceIntakeDetail(reason.detail)) {
        const identityChanged = sourceIdentityChanged(current, reason.detail)
          || (reason.detail.upload_idempotency_key !== null
            && reason.detail.upload_idempotency_key !== undefined
            && reason.detail.upload_idempotency_key !== current.upload_idempotency_key);
        if (identityChanged) {
          this.replace(clientId, {
            ...current,
            action_pending: false,
            phase: "no_longer_available",
            error: { code: "source_identity_mismatch", message: "The stale response changed immutable upload identity.", retryable: false },
          });
          return;
        }
        if (reason.detail.intake_intent !== current.intake_intent) {
          this.replace(clientId, {
            ...current,
            action_pending: false,
            phase: "no_longer_available",
            error: { code: "intake_response_intent_mismatch", message: "The stale response changed the selected upload path.", retryable: false },
          });
          return;
        }
        if (current.intake && reason.detail.version < current.intake.version) {
          const shouldPoll = !isTerminal(current.intake);
          this.replace(clientId, {
            ...current,
            action_pending: false,
            error: { code: reason.code, message: reason.message, retryable: false },
            poll_attempt: shouldPoll ? 0 : current.poll_attempt,
            next_poll_at_ms: shouldPoll ? this.now() : undefined,
          });
          if (shouldPoll) this.pumpPolls();
          return;
        }
        const shouldPoll = !isTerminal(reason.detail);
        this.replace(clientId, {
          ...current,
          action_pending: false,
          accepted: bindAcceptedSource(current.accepted, reason.detail),
          intake: reason.detail,
          error: { code: reason.code, message: reason.message, retryable: false },
          poll_attempt: shouldPoll ? 0 : current.poll_attempt,
          next_poll_at_ms: shouldPoll ? this.now() : undefined,
        });
        if (shouldPoll) this.pumpPolls();
      } else {
        const shouldPoll = !isTerminal(current.intake);
        this.replace(clientId, {
          ...current,
          action_pending: false,
          error: uploadError(reason),
          next_poll_at_ms: shouldPoll ? this.now() : undefined,
        });
        if (shouldPoll) this.pumpPolls();
      }
      return;
    }

    if (this.destroyed) return;
    const current = this.records.get(clientId);
    const exactIntakeId = current?.accepted?.intake_id;
    if (!current) return;
    if (!exactIntakeId) {
      this.replace(clientId, {
        ...current,
        action_pending: false,
        error: { code: "accepted_identity_missing", message: "Exact intake identity is unavailable.", retryable: true },
      });
      return;
    }
    try {
      const refreshed = await this.client.intake(exactIntakeId);
      if (this.destroyed) return;
      const latest = this.records.get(clientId);
      if (!latest) return;
      if (!isSourceIntakeDetail(refreshed)) {
        this.replace(clientId, {
          ...latest,
          action_pending: false,
          phase: "no_longer_available",
          error: { code: "invalid_source_intake_response", message: "The local API returned invalid intake status.", retryable: false },
        });
        return;
      }
      if (
        sourceIdentityChanged(latest, refreshed)
        || refreshed.intake_intent !== latest.intake_intent
      ) {
        this.replace(clientId, {
          ...latest,
          action_pending: false,
          phase: "no_longer_available",
          error: { code: "source_identity_mismatch", message: "The intake response changed immutable upload identity.", retryable: false },
        });
        return;
      }
      if (latest.intake && refreshed.version < latest.intake.version) {
        this.replace(clientId, {
          ...latest,
          action_pending: true,
          action_refresh_pending: true,
          error: { code: "stale_intake_response", message: "An older intake response was ignored.", retryable: true },
          poll_attempt: 0,
          next_poll_at_ms: this.now() + BASE_POLL_DELAY_MS,
        });
        this.pumpPolls();
        return;
      }
      const shouldPoll = !isTerminal(refreshed);
      this.replace(clientId, {
        ...latest,
        action_pending: false,
        accepted: bindAcceptedSource(latest.accepted, refreshed),
        intake: refreshed,
        error: undefined,
        poll_attempt: shouldPoll ? 0 : latest.poll_attempt,
        next_poll_at_ms: shouldPoll ? this.now() + BASE_POLL_DELAY_MS : undefined,
      });
      if (shouldPoll) this.pumpPolls();
    } catch (reason) {
      if (this.destroyed) return;
      const latest = this.records.get(clientId);
      if (!latest) return;
      if (reason instanceof LocalApiError && reason.status === 404) {
        this.replace(clientId, {
          ...latest,
          action_pending: false,
          phase: "no_longer_available",
          error: { code: reason.code, message: reason.message, retryable: false },
        });
        return;
      }
      this.replace(clientId, {
        ...latest,
        action_pending: true,
        action_refresh_pending: true,
        error: uploadError(reason),
        poll_attempt: 0,
        next_poll_at_ms: this.now(),
      });
      this.pumpPolls();
    }
  }

  private replace(clientId: string, item: UploadQueueItem, emit = true): void {
    this.records.set(clientId, item);
    if (emit) this.refreshSnapshot();
  }

  private refreshSnapshot(notify = true): void {
    this.snapshot = this.order.flatMap((clientId) => {
      const item = this.records.get(clientId);
      return item ? [item] : [];
    });
    this.persist();
    if (notify) for (const listener of this.listeners) listener();
  }

  private persist(): void {
    if (!this.storage) return;
    const persisted: PersistedQueueItem[] = this.snapshot.flatMap((item): PersistedQueueItem[] => {
      if (item.phase !== "accepted" && item.phase !== "upload_failed" && item.phase !== "preserving") return [];
      return [{
        client_id: item.client_id,
        intake_intent: item.intake_intent,
        upload_idempotency_key: item.upload_idempotency_key,
        file_name: item.file_name,
        file_size: item.file_size,
        file_last_modified: item.file_last_modified,
        phase: item.phase === "accepted" ? "accepted" : "upload_failed",
        accepted: item.accepted,
        intake: item.intake,
        created_at_ms: item.created_at_ms,
      }];
    }).slice(0, RECENT_INTAKE_LIMIT);
    try {
      this.storage.setItem(STORAGE_KEY, JSON.stringify(persisted));
    } catch {
      // Queue persistence is recovery metadata only; upload behavior stays usable.
    }
  }
}

export function createBrowserUploadQueue(): UploadQueue {
  let storage: QueueStorage | null = null;
  try {
    storage = typeof window === "undefined" ? null : window.localStorage;
  } catch {
    storage = null;
  }
  return new UploadQueue({ storage });
}

export function queueSummary(items: readonly UploadQueueItem[]): {
  preserving: number;
  processing: number;
  complete: number;
  failed: number;
} {
  return items.reduce((summary, item) => {
    if (item.phase === "selected" || item.phase === "preserving") summary.preserving += 1;
    else if (item.phase === "rejected" || item.phase === "upload_failed" || item.phase === "no_longer_available" || item.intake?.state === "failed") summary.failed += 1;
    else if (isTerminal(item.intake)) summary.complete += 1;
    else summary.processing += 1;
    return summary;
  }, { preserving: 0, processing: 0, complete: 0, failed: 0 });
}
