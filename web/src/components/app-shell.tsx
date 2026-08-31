import type { CSSProperties, ReactNode } from "react";

import clerkSanLockup from "../../../assets/brand/clerksan-lockup.svg?no-inline";
import clerkSanMark from "../../../assets/brand/clerksan-mark.svg?no-inline";
import humanReviewIcon from "../../../assets/brand/icons/human-review.svg?no-inline";
import intakeDocumentIcon from "../../../assets/brand/icons/intake-document.svg?no-inline";
import recurringBillIcon from "../../../assets/brand/icons/recurring-bill.svg?no-inline";
import searchEvidenceIcon from "../../../assets/brand/icons/search-evidence.svg?no-inline";
import verifiedRecordIcon from "../../../assets/brand/icons/verified-record.svg?no-inline";
import { localeLabels, supportedLocales, useI18n, type Locale } from "@/lib/i18n";

export type Route = "intake" | "mapping" | "review" | "documents" | "bills" | "search";
export type LocalServiceStatus = "ready" | "not_ready" | "unavailable";

interface NavigationItem {
  route: Exclude<Route, "mapping">;
  iconUrl: string;
}

const navigation: NavigationItem[] = [
  { route: "intake", iconUrl: intakeDocumentIcon },
  { route: "review", iconUrl: humanReviewIcon },
  { route: "documents", iconUrl: verifiedRecordIcon },
  { route: "bills", iconUrl: recurringBillIcon },
  { route: "search", iconUrl: searchEvidenceIcon },
];

const routeLabels = { intake: "nav.intake", review: "nav.review", documents: "nav.documents", bills: "nav.bills", search: "nav.search" } as const;

interface AppShellProps {
  route: Route;
  onRouteChange: (route: Route) => void;
  status: LocalServiceStatus;
  navigationDisabled?: boolean;
  children: ReactNode;
}

export function AppShell({ route, onRouteChange, status, navigationDisabled = false, children }: AppShellProps): React.ReactElement {
  const { locale, setLocale, t } = useI18n();
  const statusLabel = status === "ready" ? t("status.local_ready") : status === "not_ready" ? t("not_ready.title") : t("unavailable.title");
  return (
    <div className="app-frame">
      <a className="skip-link" href="#main-content">{t("a11y.skip_workspace")}</a>
      <aside className="side-rail" aria-label={t("a11y.navigation")}>
        <div className="brand-lockup">
          <img className="brand-lockup-image" src={clerkSanLockup} alt="Clerk-san" />
          <span className="brand-mark" aria-hidden="true"><img src={clerkSanMark} alt="" /></span>
          <span className="brand-caption">{t("brand.caption")}</span>
        </div>
        <nav className="primary-nav" aria-label={t("a11y.workspace_sections")}>
          {navigation.map(({ route: target, iconUrl }) => {
            const label = t(routeLabels[target]);
            const iconStyle = { "--nav-icon": `url("${iconUrl}")` } as CSSProperties;
            return (
              <button key={target} className={route === target ? "nav-link is-active" : "nav-link"} onClick={() => onRouteChange(target)} aria-label={label} aria-current={route === target ? "page" : undefined} disabled={navigationDisabled}>
                <span className="nav-icon" style={iconStyle} aria-hidden="true" />
                <span>{label}</span>
              </button>
            );
          })}
        </nav>
        <div className="rail-foot">
          <label className="language-picker">
            <span>{t("language.label")}</span>
            <select aria-label={t("language.label")} value={locale} onChange={(event) => setLocale(event.target.value as Locale)}>
              {supportedLocales.map((option) => <option key={option} value={option}>{localeLabels[option]}</option>)}
            </select>
          </label>
          <p>{t("sidebar.note")}</p>
        </div>
      </aside>
      <div className="workspace-column">
        <header className="workspace-header">
          <div className="local-status" aria-live="polite">
            <span className={status === "ready" ? "status-lamp is-ready" : status === "not_ready" ? "status-lamp is-not-ready" : "status-lamp"} aria-hidden="true" />
            {statusLabel}
          </div>
          <span className="header-note">{t("status.loopback_only")}</span>
        </header>
        <main id="main-content" className="workspace-main">{children}</main>
      </div>
    </div>
  );
}

export default AppShell;
