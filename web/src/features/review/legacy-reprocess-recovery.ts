const STORAGE_KEY = "clerksan.review.reprocess-recovery.v1";
const STORAGE_VERSION = 1;
const MAX_DOCUMENT_ID_LENGTH = 128;
const SAFE_DOCUMENT_ID = /^[A-Za-z0-9][A-Za-z0-9_-]*$/u;

export interface LegacyReprocessRecovery {
  documentId: string;
}

function browserStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function isDocumentId(value: unknown): value is string {
  return typeof value === "string"
    && value.length <= MAX_DOCUMENT_ID_LENGTH
    && SAFE_DOCUMENT_ID.test(value);
}

function discardInvalid(storage: Storage): null {
  try {
    storage.removeItem(STORAGE_KEY);
  } catch {
    // Invalid recovery metadata must never become an API authority.
  }
  return null;
}

export function readLegacyReprocessRecovery(): LegacyReprocessRecovery | null {
  const storage = browserStorage();
  if (!storage) return null;
  let raw: string | null;
  try {
    raw = storage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
  if (raw === null) return null;
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return discardInvalid(storage);
    const record = parsed as Record<string, unknown>;
    if (
      Object.keys(record).length !== 2
      || record.version !== STORAGE_VERSION
      || !isDocumentId(record.document_id)
    ) return discardInvalid(storage);
    return { documentId: record.document_id };
  } catch {
    return discardInvalid(storage);
  }
}

export function persistLegacyReprocessRecovery(documentId: string): void {
  const storage = browserStorage();
  if (!storage || !isDocumentId(documentId)) return;
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify({
      version: STORAGE_VERSION,
      document_id: documentId,
    }));
  } catch {
    // The current view still retains recovery when browser storage is unavailable.
  }
}

export function clearLegacyReprocessRecovery(documentId: string): void {
  const storage = browserStorage();
  if (!storage) return;
  const recovery = readLegacyReprocessRecovery();
  if (recovery?.documentId !== documentId) return;
  try {
    storage.removeItem(STORAGE_KEY);
  } catch {
    // A later successful retry can safely attempt the same idempotent clear again.
  }
}
