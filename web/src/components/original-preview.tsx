import { useEffect, useMemo, useState } from "react";
import {
  IconChevronLeft,
  IconChevronRight,
  IconDownload,
  IconFile,
  IconFileTypePdf,
  IconPhoto,
  IconRefresh,
} from "@tabler/icons-react";

import type { DocumentRecord, ExactSourcePreview, PdfPreviewManifest, ReviewItem } from "@/api/contracts";
import { api } from "@/api/client";
import { Button, LoadingPanel, Notice } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useI18n } from "@/lib/i18n";

const INLINE_ORIGINAL_MIMES = new Set(["image/jpeg", "image/png", "image/webp"]);

function exactSourceFromDocument(
  document: DocumentRecord,
  expectedDocumentId: string,
  version: number,
  sourceFileId: string,
): ExactSourcePreview | null {
  if (document.id !== expectedDocumentId) return null;
  const selected = document.files.find((file) => file.kind === "original" && file.version === version && file.id === sourceFileId);
  if (!selected || typeof selected.sha256 !== "string" || !/^[0-9a-f]{64}$/u.test(selected.sha256)) return null;
  return {
    document_id: document.id,
    source_file_id: sourceFileId,
    source_version: version,
    source_sha256: selected.sha256,
    filename: selected.source_filename ?? document.source_filename,
    mime: selected.mime ?? "",
    created_at: document.created_at,
  };
}

export function isCompletePdfPreview(manifest: PdfPreviewManifest, source: ExactSourcePreview): boolean {
  if (
    manifest.status !== "ready"
    || manifest.document_id !== source.document_id
    || manifest.source_file_id !== source.source_file_id
    || manifest.source_version !== source.source_version
    || manifest.source_sha256 !== source.source_sha256
    || manifest.page_count < 1
    || manifest.pages.length !== manifest.page_count
  ) return false;
  return manifest.pages.every((candidate, index) =>
    candidate.page_number === index + 1
    && candidate.mime === "image/png"
    && /^[0-9a-f]{64}$/u.test(candidate.sha256),
  );
}

interface OriginalPreviewProps {
  source?: ExactSourcePreview | null;
  item?: ReviewItem;
}

export function OriginalPreview({ source: suppliedSource, item }: OriginalPreviewProps): React.ReactElement {
  const { t } = useI18n();
  const [source, setSource] = useState<ExactSourcePreview | null>(suppliedSource ?? null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(Boolean(!suppliedSource && item));
  const [manifest, setManifest] = useState<PdfPreviewManifest | null>(null);
  const [page, setPage] = useState(1);

  useEffect(() => {
    setSource(suppliedSource ?? null);
    setManifest(null);
    setPage(1);
    if (suppliedSource || !item) {
      setLoading(false);
      setError(null);
      return;
    }
    let active = true;
    setLoading(true);
    setError(null);
    void api.status(item.document_id).then((document) => {
      if (!active) return;
      const exact = exactSourceFromDocument(document, item.document_id, item.source_version, item.source_file_id);
      if (!exact) throw new Error(t("original.identity_unavailable"));
      setSource(exact);
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : t("original.identity_unavailable"));
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [item, suppliedSource, t]);

  useEffect(() => {
    if (!source || source.mime !== "application/pdf") return;
    let active = true;
    setLoading(true);
    void api.pdfPreviewManifest(source.document_id, source.source_file_id, source.source_version, source.source_sha256).then((next) => {
      if (!active) return;
      if (!isCompletePdfPreview(next, source)) {
        setError(t("original.pdf_incomplete"));
        setManifest(null);
        return;
      }
      setManifest(next);
      setError(null);
    }).catch(() => {
      if (active) {
        setManifest(null);
        setError(t("original.pdf_unavailable"));
      }
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [source, t]);

  const originalUrl = useMemo(() => source ? api.originalPath(source.document_id, source.source_version, source.source_file_id, source.source_sha256) : "", [source]);
  const currentPage = manifest?.pages[page - 1];
  const pageUrl = source && currentPage ? api.pdfPreviewPagePath(source.document_id, source.source_file_id, currentPage.page_number, source.source_version, source.source_sha256) : "";
  const inlineImage = source ? INLINE_ORIGINAL_MIMES.has(source.mime) : false;

  return <section className="evidence-pane" aria-labelledby="original-title">
    <div className="panel-heading"><div><span className="eyebrow">{t("original.evidence")}</span><h2 id="original-title">{t("original.heading")}</h2></div><span className="source-version">v{source?.source_version ?? item?.source_version ?? "—"}</span></div>
    <p className="panel-copy">{t("original.copy")}</p>
    {loading ? <LoadingPanel label={t("loading.source")} /> : null}
    {error ? <Notice tone="warning">{t("original.preview_unavailable", { detail: error })}</Notice> : null}
    {!loading && source && inlineImage ? <div className="preview-frame"><img src={originalUrl} alt={t("original.image_alt", { filename: source.filename })} /></div> : null}
    {!loading && source?.mime === "application/pdf" && manifest && currentPage ? <div className="pdf-page-viewer">
      <div className="preview-frame"><img src={pageUrl} alt={t("original.pdf_page_alt", { page, count: manifest.page_count })} onError={() => { setManifest(null); setPage(1); setError(t("original.pdf_unavailable")); }} /></div>
      <div className="pdf-page-controls" aria-label={t("original.pdf_controls")}>
        <Button className="button-secondary" disabled={page <= 1} onClick={() => setPage((current) => Math.max(1, current - 1))}><IconChevronLeft size={18} />{t("action.previous")}</Button>
        <span aria-live="polite">{t("original.pdf_page", { page, count: manifest.page_count })}</span>
        <Button className="button-secondary" disabled={page >= manifest.page_count} onClick={() => setPage((current) => Math.min(manifest.page_count, current + 1))}>{t("action.next")}<IconChevronRight size={18} /></Button>
      </div>
    </div> : null}
    {!loading && source && !inlineImage && !(source.mime === "application/pdf" && manifest) ? <div className="preview-frame preview-download"><IconFile size={38} stroke={1.4} aria-hidden="true" /><p>{t("original.attachment_only")}</p></div> : null}
    <dl className="source-meta"><div><dt>{t("original.file")}</dt><dd>{source?.filename ?? t("original.fallback_filename")}</dd></div><div><dt>{t("original.type")}</dt><dd>{source?.mime || t("original.unknown_type")}</dd></div><div><dt>{t("original.captured")}</dt><dd>{formatDate(source?.created_at)}</dd></div></dl>
    {source ? <a className="button button-secondary preview-link" href={originalUrl} target="_blank" rel="noreferrer"><IconDownload size={18} aria-hidden="true" />{t("original.download")}</a> : null}
    <p className="immutable-note"><IconRefresh size={15} aria-hidden="true" />{t("original.exact_identity", { sha: source?.source_sha256.slice(0, 12) ?? "—" })}</p>
  </section>;
}

export function FilePolicyLegend(): React.ReactElement {
  const { t } = useI18n();
  return <div className="file-policy" aria-label={t("original.policy")}><span><IconPhoto size={17} />{t("original.images_inline")}</span><span><IconFileTypePdf size={17} />{t("original.pdf_sandboxed")}</span><span><IconFile size={17} />{t("original.office_download")}</span></div>;
}

export default OriginalPreview;
