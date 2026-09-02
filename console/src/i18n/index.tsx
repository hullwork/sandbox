import { Globe } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { en, type EnglishMessages, type TranslationKey } from "./locales/en";
import { zhCN } from "./locales/zh-CN";
import {
  LANGUAGE_KEY,
  LOCALES,
  type Locale,
  type PluralTranslationKey,
} from "./config";

export { LOCALES };
export type { Locale };

const MESSAGES: Record<Locale, EnglishMessages> = {
  en,
  "zh-CN": zhCN,
};

export type TranslationValues = Record<string, string | number>;
export type Translator = (
  key: TranslationKey,
  values?: TranslationValues,
) => string;

export type LocalizedError =
  | { key: TranslationKey; values?: TranslationValues }
  | { message: string };

interface I18nContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: Translator;
  tPlural: (
    key: PluralTranslationKey,
    count: number,
    values?: TranslationValues,
  ) => string;
  formatTime: (value: Date) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

function normalizeLocale(candidate: string | undefined): Locale | null {
  if (!candidate) {
    return null;
  }
  const normalized = candidate.toLowerCase();
  if (normalized === "en" || normalized.startsWith("en-")) {
    return "en";
  }
  if (normalized === "zh" || normalized.startsWith("zh-")) {
    return "zh-CN";
  }
  return null;
}

function detectInitialLocale(): Locale {
  try {
    const saved = normalizeLocale(window.localStorage.getItem(LANGUAGE_KEY) ?? undefined);
    if (saved) {
      return saved;
    }
  } catch {
    // Storage may be unavailable in private-mode browsers. Browser language is
    // still a useful fallback, and the user can switch explicitly in the UI.
  }

  const candidates = typeof navigator === "undefined"
    ? []
    : [navigator.language, ...(navigator.languages ?? [])];
  for (const candidate of candidates) {
    const locale = normalizeLocale(candidate);
    if (locale) {
      return locale;
    }
  }
  return "en";
}

function interpolate(message: string, values: TranslationValues): string {
  return message.replace(/\{(\w+)\}/g, (match, name: string) => (
    Object.hasOwn(values, name) ? String(values[name]) : match
  ));
}

/**
 * Provides locale state to the console. The choice is explicit once set, but the
 * first visit follows the browser language so international users do not have to
 * find a control written in a language they cannot read.
 */
export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocale] = useState<Locale>(detectInitialLocale);

  const t = useCallback((key: TranslationKey, values?: TranslationValues) => {
    const message = MESSAGES[locale][key] ?? en[key];
    return values ? interpolate(message, values) : message;
  }, [locale]);

  const selectLocale = useCallback((next: Locale) => {
    setLocale(next);
    try {
      window.localStorage.setItem(LANGUAGE_KEY, next);
    } catch {
      // Storage can be unavailable; the selected locale still applies in memory.
    }
  }, []);

  const tPlural = useCallback((
    key: PluralTranslationKey,
    count: number,
    values?: TranslationValues,
  ) => {
    const category = new Intl.PluralRules(locale).select(count);
    const pluralKey = `${key}.${category}` as TranslationKey;
    const fallbackKey = `${key}.other` as TranslationKey;
    const message = MESSAGES[locale][pluralKey] ?? MESSAGES[locale][fallbackKey];
    return interpolate(message, { ...values, count });
  }, [locale]);

  useEffect(() => {
    document.documentElement.lang = locale;
    document
      .querySelector('meta[name="description"]')
      ?.setAttribute("content", t("app.description"));
  }, [locale, t]);

  const formatTime = useCallback((value: Date) => value.toLocaleTimeString(locale, {
    hour12: false,
  }), [locale]);

  const value = useMemo(() => ({
    locale,
    setLocale: selectLocale,
    t,
    tPlural,
    formatTime,
  }), [formatTime, locale, selectLocale, t, tPlural]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const context = useContext(I18nContext);
  if (!context) {
    throw new Error("useI18n must be used inside I18nProvider");
  }
  return context;
}

/** Renders either a translatable local error or an opaque backend message. */
export function errorText(error: LocalizedError, t: Translator): string {
  return "key" in error ? t(error.key, error.values) : error.message;
}

export function LanguageSwitcher({ className }: { className?: string }) {
  const { locale, setLocale, t } = useI18n();

  return (
    <label className={`language-switcher${className ? ` ${className}` : ""}`}>
      <Globe size={15} aria-hidden="true" />
      <span className="visually-hidden">{t("language.label")}</span>
      <select
        value={locale}
        aria-label={t("language.label")}
        onChange={(event) => setLocale(event.target.value as Locale)}
      >
        <option value="en">{t("language.english")}</option>
        <option value="zh-CN">{t("language.chinese")}</option>
      </select>
    </label>
  );
}
