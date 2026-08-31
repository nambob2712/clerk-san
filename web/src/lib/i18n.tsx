import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

import { localeLabels, supportedLocales, translations, type Locale, type TranslationKey } from "@/generated/translations";

interface I18nContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: TranslationKey, values?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

function interpolate(template: string, values: Record<string, string | number> | undefined): string {
  return values ? template.replace(/\{([^}]+)\}/gu, (_, name: string) => String(values[name] ?? `{${name}}`)) : template;
}

export function I18nProvider({ children }: { children: ReactNode }): React.ReactElement {
  const [locale, setLocale] = useState<Locale>("en");
  const value = useMemo<I18nContextValue>(
    () => ({
      locale,
      setLocale,
      t: (key, values) => interpolate(translations[locale][key] ?? translations.en[key] ?? key, values),
    }),
    [locale],
  );
  return <I18nContext value={value}>{children}</I18nContext>;
}

export function useI18n(): I18nContextValue {
  const context = useContext(I18nContext);
  if (!context) throw new Error("useI18n must be used inside I18nProvider.");
  return context;
}

export { localeLabels, supportedLocales };
export type { Locale, TranslationKey };
