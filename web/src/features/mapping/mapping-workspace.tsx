import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { IconArrowLeft, IconCheck, IconReload, IconTable, IconWand } from "@tabler/icons-react";

import type {
  DateStyle,
  DecimalStyle,
  FieldParser,
  FinancialSubtype,
  MappingFieldRule,
  MappingSetDraft,
  MappingSetEntryDraft,
  MappingSetPreview,
  MappingSourceRef,
  RecordKind,
  SchemaDescriptor,
  SchemaMapping,
} from "@/api/contracts";
import { api, LocalApiError } from "@/api/client";
import { Button, EmptyState, LoadingPanel, Notice, PageHeading } from "@/components/ui";
import { useI18n, type TranslationKey } from "@/lib/i18n";

type TableAction = "map" | "ignore";

interface RuleDraft {
  sourceColumn: string;
  targetField: string;
  parser: FieldParser;
  dateStyle: DateStyle;
  decimalStyle: DecimalStyle;
  required: boolean;
}

interface TableDraft {
  schemaFingerprint: string;
  action: TableAction;
  mappingId: string;
  recordKind: RecordKind;
  financialSubtype: FinancialSubtype | "";
  ignoreReason: string;
  rules: RuleDraft[];
}

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
const CANONICAL_FIELDS = ["transaction_date", "total_amount", "counterparty", "currency", "category", "expense_kind", "due_date", "registration_number", "tax_8_amount", "tax_10_amount", "memo", "title", "text", "source_locator"];

function uuid(): string {
  return globalThis.crypto?.randomUUID?.() ?? `local-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function initialDraft(descriptor: SchemaDescriptor): TableDraft {
  return {
    schemaFingerprint: descriptor.schema_fingerprint,
    action: "map",
    mappingId: "",
    recordKind: "generic_document",
    financialSubtype: "",
    ignoreReason: "",
    rules: descriptor.ordered_headers.map((header) => ({
      sourceColumn: header,
      targetField: header,
      parser: "raw",
      dateStyle: "iso",
      decimalStyle: "dot",
      required: false,
    })),
  };
}

function mappingRule(rule: RuleDraft): MappingFieldRule {
  return {
    target_field: rule.targetField.trim(),
    source_columns: [rule.sourceColumn],
    parser: rule.parser,
    trim: true,
    null_markers: [],
    value_map: [],
    sign_rule: "preserve",
    currency_aliases: [],
    date_style: rule.parser === "date" ? rule.dateStyle : null,
    decimal_style: rule.parser === "decimal" ? rule.decimalStyle : null,
  };
}

function sameSource(left: MappingSourceRef, right: MappingSourceRef): boolean {
  return left.source_intake_id === right.source_intake_id
    && left.source_file_id === right.source_file_id
    && left.source_version === right.source_version
    && left.source_sha256 === right.source_sha256
    && left.normalized_sha256 === right.normalized_sha256
    && left.structure_fingerprint === right.structure_fingerprint;
}

export function MappingWorkspace({ documentId, onApplied, onApplyLockChange }: { documentId: string; onApplied: (batchId: string) => void; onApplyLockChange?: (locked: boolean) => void }): React.ReactElement {
  const { t } = useI18n();
  const tRef = useRef(t);
  tRef.current = t;
  const [descriptors, setDescriptors] = useState<SchemaDescriptor[]>([]);
  const [source, setSource] = useState<Awaited<ReturnType<typeof api.schemaDescriptors>>["source"] | null>(null);
  const [mappings, setMappings] = useState<SchemaMapping[]>([]);
  const [drafts, setDrafts] = useState<Record<string, TableDraft>>({});
  const [actor, setActor] = useState("local-user");
  const [preview, setPreview] = useState<MappingSetPreview | null>(null);
  const [resolvedEntries, setResolvedEntries] = useState<MappingSetEntryDraft[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState<"preview" | "apply" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [draftReset, setDraftReset] = useState(false);
  const mappingKeys = useRef<Record<string, string>>({});
  const setKey = useRef(uuid());
  const applyKey = useRef(uuid());
  const draftRevision = useRef(0);
  const loadRequest = useRef(0);
  const [previewRevision, setPreviewRevision] = useState<number | null>(null);

  const load = useCallback(async (preserveDrafts = true) => {
    const requestId = ++loadRequest.current;
    draftRevision.current += 1;
    setLoading(true);
    setError(null);
    try {
      const [descriptorResponse, mappingResponse] = await Promise.all([
        api.schemaDescriptors(documentId),
        api.mappings(documentId),
      ]);
      if (loadRequest.current !== requestId) return;
      const mismatchedSource = descriptorResponse.document_id !== documentId
        || mappingResponse.document_id !== documentId
        || !sameSource(descriptorResponse.source, mappingResponse.source)
        || mappingResponse.items.some((mapping) => !sameSource(mapping.source, mappingResponse.source));
      if (mismatchedSource) {
        setStale(true);
        throw new Error(tRef.current("mapping.stale"));
      }
      setDescriptors(descriptorResponse.descriptors);
      setSource(descriptorResponse.source);
      setMappings(mappingResponse.items);
      setDrafts((current) => {
        let resetIncompatible = false;
        const next = Object.fromEntries(descriptorResponse.descriptors.map((descriptor) => {
          const previous = current[descriptor.table_locator];
          const columnsMatch = previous?.rules.length === descriptor.ordered_headers.length
            && previous.rules.every((rule, index) => rule.sourceColumn === descriptor.ordered_headers[index]);
          const mappingStillCompatible = !previous?.mappingId || mappingResponse.items.some((mapping) =>
            mapping.id === previous.mappingId
            && mapping.table_locator === descriptor.table_locator
            && mapping.schema_fingerprint === descriptor.schema_fingerprint
            && sameSource(mapping.source, descriptorResponse.source),
          );
          const compatible = Boolean(
            preserveDrafts
            && previous
            && previous.schemaFingerprint === descriptor.schema_fingerprint
            && columnsMatch
            && mappingStillCompatible,
          );
          if (preserveDrafts && previous && !compatible) resetIncompatible = true;
          return [descriptor.table_locator, compatible ? previous : initialDraft(descriptor)];
        }));
        setDraftReset(resetIncompatible);
        if (resetIncompatible) {
          mappingKeys.current = {};
          setKey.current = uuid();
          applyKey.current = uuid();
        }
        return next;
      });
      setPreview(null);
      setResolvedEntries(null);
      setPreviewRevision(null);
      setStale(false);
    } catch (reason) {
      if (loadRequest.current === requestId) setError(reason instanceof Error ? reason.message : tRef.current("mapping.load_failed"));
    } finally {
      if (loadRequest.current === requestId) setLoading(false);
    }
  }, [documentId]);

  useEffect(() => { void load(false); }, [load]);

  const updateDraft = (locator: string, change: Partial<TableDraft>): void => {
    setDrafts((current) => ({ ...current, [locator]: { ...current[locator], ...change } }));
    draftRevision.current += 1;
    delete mappingKeys.current[locator];
    setKey.current = uuid();
    applyKey.current = uuid();
    setPreview(null);
    setResolvedEntries(null);
    setPreviewRevision(null);
  };

  const updateRule = (locator: string, index: number, change: Partial<RuleDraft>): void => {
    const table = drafts[locator];
    if (!table) return;
    updateDraft(locator, { rules: table.rules.map((rule, ruleIndex) => ruleIndex === index ? { ...rule, ...change } : rule) });
  };

  const createEntries = async (): Promise<MappingSetEntryDraft[]> => {
    if (!source || !actor.trim()) throw new Error(t("mapping.actor_required"));
    const entries: MappingSetEntryDraft[] = [];
    for (const descriptor of descriptors) {
      const draft = drafts[descriptor.table_locator];
      if (!draft) throw new Error(t("mapping.incomplete"));
      if (draft.action === "ignore") {
        if (!draft.ignoreReason.trim()) throw new Error(t("mapping.ignore_reason_required"));
        entries.push({ table_locator: descriptor.table_locator, schema_fingerprint: descriptor.schema_fingerprint, ignore_reason: draft.ignoreReason.trim() });
        continue;
      }
      let selected = mappings.find((mapping) => mapping.id === draft.mappingId
        && mapping.table_locator === descriptor.table_locator
        && mapping.schema_fingerprint === descriptor.schema_fingerprint
        && sameSource(mapping.source, source));
      if (!selected) {
        if (draft.recordKind === "financial" && !draft.financialSubtype) throw new Error(t("mapping.financial_subtype_required"));
        const activeRules = draft.rules.filter((rule) => rule.targetField.trim());
        if (activeRules.length === 0) throw new Error(t("mapping.field_required"));
        const targets = activeRules.map((rule) => rule.targetField.trim());
        if (new Set(targets).size !== targets.length) throw new Error(t("mapping.duplicate_target"));
        const financialSubtype: FinancialSubtype | null = draft.recordKind === "financial" ? draft.financialSubtype || null : null;
        mappingKeys.current[descriptor.table_locator] ??= uuid();
        selected = await api.createMapping(documentId, {
          source,
          idempotency_key: mappingKeys.current[descriptor.table_locator],
          table_locator: descriptor.table_locator,
          schema_fingerprint: descriptor.schema_fingerprint,
          record_kind: draft.recordKind,
          financial_subtype: financialSubtype,
          field_rules: activeRules.map(mappingRule),
          required_fields: activeRules.filter((rule) => rule.required).map((rule) => rule.targetField.trim()),
          created_by: actor.trim(),
        });
        if (!sameSource(selected.source, source)
          || selected.table_locator !== descriptor.table_locator
          || selected.schema_fingerprint !== descriptor.schema_fingerprint) {
          throw new Error(t("mapping.stale"));
        }
        setMappings((current) => current.some((mapping) => mapping.id === selected?.id) ? current : [...current, selected as SchemaMapping]);
      }
      entries.push({
        table_locator: descriptor.table_locator,
        schema_fingerprint: descriptor.schema_fingerprint,
        mapping_id: selected.id,
        mapping_version: selected.mapping_version,
      });
    }
    return entries;
  };

  const handleFailure = (reason: unknown, fallback: string): void => {
    if (reason instanceof LocalApiError && reason.isStaleMapping) setStale(true);
    setError(reason instanceof Error ? reason.message : fallback);
  };

  const validatePreview = async (): Promise<void> => {
    if (!source) return;
    const revision = draftRevision.current;
    setWorking("preview"); setError(null); setStale(false);
    try {
      const entries = await createEntries();
      if (draftRevision.current !== revision) return;
      const next = await api.previewMappingSet(documentId, { source, idempotency_key: setKey.current, entries, created_by: actor.trim(), preview_limit: 50 });
      if (draftRevision.current !== revision) return;
      if (next.document_id !== documentId || !sameSource(next.source, source)) throw new Error(t("mapping.stale"));
      setResolvedEntries(entries);
      setPreview(next);
      setPreviewRevision(revision);
    } catch (reason) { handleFailure(reason, t("mapping.preview_failed")); }
    finally { setWorking(null); }
  };

  const apply = async (): Promise<void> => {
    if (!source || !preview || !resolvedEntries || previewRevision !== draftRevision.current) return;
    setWorking("apply"); setError(null); setStale(false); onApplyLockChange?.(true);
    try {
      const body: MappingSetDraft = { source, idempotency_key: setKey.current, entries: resolvedEntries, created_by: actor.trim(), preview_limit: 50 };
      const mappingSet = await api.createMappingSet(documentId, body);
      const expectedVersions = Object.fromEntries(mappingSet.entries.flatMap((entry) => entry.mapping_id && entry.mapping_version ? [[entry.mapping_id, entry.mapping_version]] : []));
      const batch = await api.applyMappingSet(documentId, mappingSet.id, {
        source,
        mapping_set_version: mappingSet.version,
        mapping_set_digest: mappingSet.set_digest,
        expected_mapping_versions: expectedVersions,
        idempotency_key: applyKey.current,
      });
      onApplied(batch.id);
    } catch (reason) { handleFailure(reason, t("mapping.apply_failed")); }
    finally { setWorking(null); onApplyLockChange?.(false); }
  };

  const totalRows = useMemo(() => descriptors.reduce((total, descriptor) => total + descriptor.row_count, 0), [descriptors]);
  const applying = working === "apply";
  const missingFinancialSubtype = descriptors.some((descriptor) => {
    const draft = drafts[descriptor.table_locator];
    return draft?.action === "map" && !draft.mappingId && draft.recordKind === "financial" && !draft.financialSubtype;
  });

  return <div className="page-stack">
    <PageHeading title={t("mapping.title")} copy={t("mapping.copy")} action={<a className="button button-secondary" href="#intake" aria-disabled={applying} tabIndex={applying ? -1 : undefined} onClick={(event) => { if (applying) event.preventDefault(); }}><IconArrowLeft size={18} />{t("mapping.back")}</a>} />
    {error ? <Notice tone="error" onDismiss={() => setError(null)}>{error}</Notice> : null}
    {stale ? <Notice tone="warning"><strong>{t("mapping.stale")}</strong><Button className="button-secondary" onClick={() => void load(true)} disabled={applying}><IconReload size={18} />{t("mapping.reload_preserve")}</Button></Notice> : null}
    {draftReset ? <Notice tone="warning" onDismiss={() => setDraftReset(false)}>{t("mapping.incompatible_draft_reset")}</Notice> : null}
    {loading ? <LoadingPanel label={t("mapping.loading")} /> : null}
    {!loading && descriptors.length === 0 ? <EmptyState title={t("mapping.empty_title")} copy={t("mapping.empty_copy")} /> : null}
    {descriptors.length ? <>
      <section className="metrics-strip" aria-label={t("mapping.summary")}><div><span>{t("mapping.structures")}</span><strong>{descriptors.length}</strong></div><div><span>{t("mapping.rows")}</span><strong>{totalRows}</strong></div><div><span>{t("mapping.candidates")}</span><strong>{preview?.candidate_count ?? "—"}</strong></div></section>
      <label className="mapping-actor"><span>{t("review.reviewer")}</span><input value={actor} onChange={(event) => setActor(event.target.value)} disabled={applying} /></label>
      {descriptors.map((descriptor) => {
        const draft = drafts[descriptor.table_locator];
        if (!draft) return null;
        const existing = mappings.filter((mapping) => mapping.table_locator === descriptor.table_locator
          && mapping.schema_fingerprint === descriptor.schema_fingerprint
          && source !== null
          && sameSource(mapping.source, source));
        return <section className="mapping-card" key={descriptor.table_locator}>
          <div className="panel-heading"><div><span className="eyebrow">{t("mapping.structure")}</span><h2><IconTable size={20} />{descriptor.table_locator}</h2></div><span className="status-chip">{t("mapping.row_count", { count: descriptor.row_count })}</span></div>
          <div className="mapping-mode">
            <label><input type="radio" name={`mode-${descriptor.table_locator}`} checked={draft.action === "map"} onChange={() => updateDraft(descriptor.table_locator, { action: "map" })} disabled={applying} />{t("mapping.map_structure")}</label>
            <label><input type="radio" name={`mode-${descriptor.table_locator}`} checked={draft.action === "ignore"} onChange={() => updateDraft(descriptor.table_locator, { action: "ignore" })} disabled={applying} />{t("mapping.ignore_structure")}</label>
          </div>
          {draft.action === "ignore" ? <label><span>{t("mapping.ignore_reason")}</span><textarea value={draft.ignoreReason} onChange={(event) => updateDraft(descriptor.table_locator, { ignoreReason: event.target.value })} disabled={applying} /></label> : <>
            {existing.length ? <label><span>{t("mapping.saved_definition")}</span><select value={draft.mappingId} onChange={(event) => updateDraft(descriptor.table_locator, { mappingId: event.target.value })} disabled={applying}><option value="">{t("mapping.create_definition")}</option>{existing.map((mapping) => <option key={mapping.id} value={mapping.id}>{t(RECORD_KIND_KEYS[mapping.record_kind])} · v{mapping.mapping_version} · {mapping.id.slice(0, 8)}</option>)}</select></label> : null}
            {!draft.mappingId ? <>
              <div className="mapping-classification"><label><span>{t("mapping.record_kind")}</span><select value={draft.recordKind} onChange={(event) => updateDraft(descriptor.table_locator, { recordKind: event.target.value as RecordKind, financialSubtype: "" })} disabled={applying}><option value="generic_document">{t("mapping.generic")}</option><option value="financial">{t("mapping.financial")}</option></select></label>{draft.recordKind === "financial" ? <label><span>{t("mapping.financial_subtype")}</span><select value={draft.financialSubtype} onChange={(event) => updateDraft(descriptor.table_locator, { financialSubtype: event.target.value as FinancialSubtype | "" })} disabled={applying}><option value="">{t("mapping.select_financial_subtype")}</option>{FINANCIAL_SUBTYPES.map((subtype) => <option key={subtype} value={subtype}>{t(FINANCIAL_SUBTYPE_KEYS[subtype])}</option>)}</select></label> : null}</div>
              <div className="data-table-wrap"><table><thead><tr><th>{t("mapping.source_column")}</th><th>{t("mapping.inferred_type")}</th><th>{t("mapping.destination_field")}</th><th>{t("mapping.transform")}</th><th>{t("mapping.required")}</th></tr></thead><tbody>{draft.rules.map((rule, index) => <tr key={rule.sourceColumn}><td>{rule.sourceColumn}</td><td>{descriptor.inferred_types[index] ?? "—"}</td><td><input aria-label={t("mapping.destination_for", { field: rule.sourceColumn })} list="canonical-fields" value={rule.targetField} onChange={(event) => updateRule(descriptor.table_locator, index, { targetField: event.target.value })} disabled={applying} /></td><td><select aria-label={t("mapping.transform_for", { field: rule.sourceColumn })} value={rule.parser} onChange={(event) => updateRule(descriptor.table_locator, index, { parser: event.target.value as FieldParser })} disabled={applying}><option value="raw">raw</option><option value="date">date</option><option value="decimal">decimal</option><option value="currency">currency</option></select>{rule.parser === "date" ? <select aria-label={t("mapping.date_style")} value={rule.dateStyle} onChange={(event) => updateRule(descriptor.table_locator, index, { dateStyle: event.target.value as DateStyle })} disabled={applying}><option value="iso">ISO</option><option value="ymd_slash">Y/M/D</option><option value="dmy_slash">D/M/Y</option><option value="mdy_slash">M/D/Y</option><option value="japanese">日本語</option></select> : null}{rule.parser === "decimal" ? <select aria-label={t("mapping.decimal_style")} value={rule.decimalStyle} onChange={(event) => updateRule(descriptor.table_locator, index, { decimalStyle: event.target.value as DecimalStyle })} disabled={applying}><option value="dot">1,234.56</option><option value="comma">1.234,56</option></select> : null}</td><td><input type="checkbox" checked={rule.required} onChange={(event) => updateRule(descriptor.table_locator, index, { required: event.target.checked })} aria-label={t("mapping.required_field", { field: rule.targetField || rule.sourceColumn })} disabled={applying} /></td></tr>)}</tbody></table></div>
            </> : <Notice tone="info">{t("mapping.using_saved")}</Notice>}
          </>}
        </section>;
      })}
      <datalist id="canonical-fields">{CANONICAL_FIELDS.map((field) => <option key={field} value={field} />)}</datalist>
      <div className="mapping-actions"><Button className="button-secondary" onClick={() => void validatePreview()} disabled={working !== null || missingFinancialSubtype}><IconWand size={18} />{working === "preview" ? t("mapping.validating") : t("mapping.validate_preview")}</Button><Button className="button-primary" onClick={() => void apply()} disabled={!preview || previewRevision !== draftRevision.current || working !== null}><IconCheck size={18} />{working === "apply" ? t("mapping.applying") : t("mapping.apply")}</Button></div>
      {preview ? <section className="mapping-preview" aria-live="polite"><div className="panel-heading"><div><span className="eyebrow">{t("mapping.validation")}</span><h2>{t("mapping.preview_title")}</h2></div><span className="status-chip">{t("mapping.candidate_count", { count: preview.candidate_count })}</span></div>{preview.previews.map((table) => <article key={table.table_locator}><h3>{table.table_locator}</h3><p>{t("mapping.validation_counts", { valid: table.valid_rows, errors: table.error_rows, blank: table.blank_rows, total: table.total_rows })}</p><div className="data-table-wrap"><table><thead><tr><th>{t("mapping.source_locator")}</th><th>{t("mapping.values")}</th><th>{t("mapping.errors")}</th></tr></thead><tbody>{table.rows.map((row) => <tr key={`${table.table_locator}:${row.row_ordinal}`}><td>{row.source_locator}</td><td><code>{JSON.stringify(row.values)}</code></td><td>{row.errors.join(", ") || "—"}</td></tr>)}</tbody></table></div>{table.truncated ? <p className="muted-copy">{t("mapping.preview_truncated")}</p> : null}</article>)}</section> : null}
    </> : null}
  </div>;
}

export default MappingWorkspace;
