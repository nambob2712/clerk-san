import type { JsonValue } from "@/api/contracts";

export function asText(value: JsonValue | undefined | null): string {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

export function fieldValue(value: JsonValue | undefined): JsonValue | undefined {
  if (isRecord(value) && "value" in value) return value.value;
  return value;
}

export function fieldConfidence(value: JsonValue | undefined): number | undefined {
  if (isRecord(value) && typeof value.confidence === "number") return value.confidence;
  return undefined;
}

export function fieldSource(value: JsonValue | undefined): string | undefined {
  if (isRecord(value) && typeof value.source_span === "string") return value.source_span;
  return undefined;
}

export function editValue(value: JsonValue | undefined): string {
  const raw = fieldValue(value);
  if (raw === undefined || raw === null) return "";
  return typeof raw === "object" ? JSON.stringify(raw) : String(raw);
}

export function parseEdit(value: string): JsonValue {
  const trimmed = value.trim();
  if (!trimmed) return "";
  try {
    return JSON.parse(trimmed) as JsonValue;
  } catch {
    return trimmed;
  }
}

export function isRecord(value: unknown): value is Record<string, JsonValue | undefined> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleDateString();
}

export function formatMoney(value: unknown, currency = "JPY"): string {
  const amount = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(amount)) return "";
  return new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: 2 }).format(amount);
}
