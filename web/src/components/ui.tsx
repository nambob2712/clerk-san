import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { IconAlertTriangle, IconCheck, IconRefresh, IconX } from "@tabler/icons-react";

export function PageHeading({ title, copy, action }: { title: string; copy: string; action?: ReactNode }): React.ReactElement {
  return <section className="page-heading"><div><h1>{title}</h1><p>{copy}</p></div>{action}</section>;
}

export function Notice({ tone = "info", children, onDismiss }: { tone?: "info" | "warning" | "error" | "success"; children: ReactNode; onDismiss?: () => void }): React.ReactElement {
  const { t } = useI18n();
  const Icon = tone === "success" ? IconCheck : tone === "error" || tone === "warning" ? IconAlertTriangle : IconRefresh;
  return <div className={`notice notice-${tone}`} role={tone === "error" ? "alert" : "status"}><Icon size={19} aria-hidden="true" /><div>{children}</div>{onDismiss ? <button className="icon-button" onClick={onDismiss} aria-label={t("a11y.dismiss")}><IconX size={18} /></button> : null}</div>;
}

export const Button = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement>>(function Button(
  { children, className = "", ...props },
  ref,
): React.ReactElement {
  return <button ref={ref} className={`button ${className}`.trim()} {...props}>{children}</button>;
});

export function EmptyState({ title, copy, action }: { title: string; copy: string; action?: ReactNode }): React.ReactElement {
  return <div className="empty-state"><strong>{title}</strong><p>{copy}</p>{action}</div>;
}

export function LoadingPanel({ label }: { label: string }): React.ReactElement {
  return <div className="loading-panel" aria-live="polite"><span className="loading-bar" aria-hidden="true" /><span>{label}</span></div>;
}
import { useI18n } from "@/lib/i18n";
