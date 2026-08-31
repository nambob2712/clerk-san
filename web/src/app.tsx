import { useCallback, useEffect, useRef, useState } from "react";

import { api, LocalApiError } from "@/api/client";
import { AppShell, type LocalServiceStatus, type Route } from "@/components/app-shell";
import { Notice } from "@/components/ui";
import BillsView from "@/features/bills/bills-view";
import DocumentsView from "@/features/documents/documents-view";
import IntakeView from "@/features/intake/intake-view";
import { UploadQueueProvider } from "@/features/intake/upload-queue-provider";
import MappingWorkspace from "@/features/mapping/mapping-workspace";
import ReviewWorkspace from "@/features/review/review-workspace";
import SearchView from "@/features/search/search-view";
import { I18nProvider, useI18n } from "@/lib/i18n";

const routes = new Set<Route>(["intake", "mapping", "review", "documents", "bills", "search"]);

interface WorkspaceLocation {
  route: Route;
  mappingDocumentId: string | null;
}

function parsedLocation(): WorkspaceLocation {
  const [candidate, encoded] = window.location.hash.slice(1).split("/");
  const route = routes.has(candidate as Route) ? candidate as Route : "review";
  if (route !== "mapping") return { route, mappingDocumentId: null };
  if (!encoded) return { route: "intake", mappingDocumentId: null };
  try {
    const mappingDocumentId = decodeURIComponent(encoded);
    return mappingDocumentId.trim() ? { route, mappingDocumentId } : { route: "intake", mappingDocumentId: null };
  } catch {
    return { route: "intake", mappingDocumentId: null };
  }
}

interface AvailabilityNotice {
  title: string;
  detail: string;
  startLocal: boolean;
}

function Workspace(): React.ReactElement {
  const { t } = useI18n();
  const [location, setLocation] = useState<WorkspaceLocation>(parsedLocation);
  const [status, setStatus] = useState<LocalServiceStatus>("unavailable");
  const [intakeEnabled, setIntakeEnabled] = useState(false);
  const [processingDelayed, setProcessingDelayed] = useState(false);
  const [universalFormatsAvailable, setUniversalFormatsAvailable] = useState(false);
  const [processFormats, setProcessFormats] = useState<string[]>([]);
  const [availability, setAvailability] = useState<AvailabilityNotice | null>(null);
  const [, setReviewRevision] = useState(0);
  const [navigationDisabled, setNavigationDisabled] = useState(false);
  const availabilityRequest = useRef(0);
  const navigationDisabledRef = useRef(false);
  const lockedHash = useRef("");
  const tRef = useRef(t);
  tRef.current = t;
  const checkAvailability = useCallback(async () => {
    const requestId = ++availabilityRequest.current;
    try {
      const health = await api.health();
      const readiness = await api.ready();
      const capabilities = await api.capabilities().catch(() => null);
      // Additive processing components describe delayed background work. The
      // compatible intake gate remains the successful top-level readiness result.
      const compatibleIntake = readiness.intake_ready === true;
      if (health.status !== "ok" || (readiness.status !== "ready" && !compatibleIntake)) throw new Error(tRef.current("not_ready.title"));
      if (availabilityRequest.current !== requestId) return;
      setStatus(readiness.status === "ready" ? "ready" : "not_ready");
      setIntakeEnabled(readiness.intake_ready !== false);
      setProcessingDelayed(readiness.processing_ready === false);
      const advertisedFormats = readiness.universal_processing_ready && capabilities?.sandbox_verified
        ? capabilities.process
        : [];
      setProcessFormats(advertisedFormats);
      setUniversalFormatsAvailable(Boolean(
        readiness.universal_processing_ready
        && capabilities?.sandbox_verified
        && capabilities.process.includes("csv")
        && capabilities.process.includes("xlsx"),
      ));
      setAvailability(null);
    } catch (reason) {
      if (availabilityRequest.current !== requestId) return;
      const isNotReady = reason instanceof LocalApiError && ["not_ready", "local_data_needs_upgrade"].includes(reason.code);
      setStatus(isNotReady ? "not_ready" : "unavailable");
      setIntakeEnabled(false);
      setProcessingDelayed(false);
      setUniversalFormatsAvailable(false);
      setProcessFormats([]);
      setAvailability(isNotReady
        ? { title: tRef.current("not_ready.title"), detail: reason.code === "local_data_needs_upgrade" ? tRef.current("not_ready.caption") : reason.message, startLocal: false }
        : { title: tRef.current("unavailable.title"), detail: reason instanceof Error ? reason.message : tRef.current("unavailable.title"), startLocal: true });
    }
  }, []);
  useEffect(() => {
    void checkAvailability();
    return () => { availabilityRequest.current += 1; };
  }, [checkAvailability]);
  useEffect(() => {
    const sync = () => {
      if (navigationDisabledRef.current) {
        if (lockedHash.current && window.location.hash !== lockedHash.current) window.history.replaceState(window.history.state, "", lockedHash.current);
        return;
      }
      const next = parsedLocation();
      if (next.route === "intake" && window.location.hash.slice(1).split("/")[0] === "mapping") window.history.replaceState(window.history.state, "", "#intake");
      setLocation(next);
    };
    window.addEventListener("hashchange", sync);
    sync();
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  const commitRoute = useCallback((next: Route): void => {
    window.location.hash = next;
    setLocation({ route: next, mappingDocumentId: null });
  }, []);
  const changeRoute = useCallback((next: Route): void => {
    if (!navigationDisabledRef.current) commitRoute(next);
  }, [commitRoute]);
  const notifyReviewChanged = useCallback((): void => {
    setReviewRevision((revision) => revision + 1);
  }, []);
  const setMappingApplyLock = useCallback((locked: boolean): void => {
    navigationDisabledRef.current = locked;
    lockedHash.current = locked ? window.location.hash : "";
    setNavigationDisabled(locked);
  }, []);
  const completeMappingApply = useCallback((): void => {
    navigationDisabledRef.current = false;
    lockedHash.current = "";
    setNavigationDisabled(false);
    notifyReviewChanged();
    commitRoute("review");
  }, [commitRoute, notifyReviewChanged]);
  const { route, mappingDocumentId } = location;
  const content = route === "intake"
    ? <IntakeView intakeEnabled={intakeEnabled} processingDelayed={processingDelayed} universalFormatsAvailable={universalFormatsAvailable} processFormats={processFormats} onReviewChanged={notifyReviewChanged} />
    : route === "mapping" && mappingDocumentId
      ? <MappingWorkspace key={mappingDocumentId} documentId={mappingDocumentId} onApplyLockChange={setMappingApplyLock} onApplied={completeMappingApply} />
      : route === "review"
        ? <ReviewWorkspace onReviewChanged={notifyReviewChanged} />
        : route === "documents"
          ? <DocumentsView />
          : route === "bills"
            ? <BillsView />
            : <SearchView />;
  return <AppShell route={route} onRouteChange={changeRoute} status={status} navigationDisabled={navigationDisabled}>{availability ? <Notice tone="warning">{availability.title}: {availability.detail}{availability.startLocal ? ` ${t("unavailable.start_local")}` : null}</Notice> : null}{content}</AppShell>;
}

export default function App(): React.ReactElement { return <I18nProvider><UploadQueueProvider><Workspace /></UploadQueueProvider></I18nProvider>; }
