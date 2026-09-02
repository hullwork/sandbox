/**
 * Locale configuration shared by the React provider and validation tooling.
 *
 * Keep this file free of React imports so the Node-based i18n checker can parse it.
 */
export const LOCALES = ["en", "zh-CN"] as const;

export type Locale = (typeof LOCALES)[number];

export const LANGUAGE_KEY = "sandbox-console-language";

/** Count message families that use the provider's plural resolver. */
export const PLURAL_KEYS = [
  "common.count",
  "common.records",
  "sandbox.count",
  "workspace.count",
  "templates.count",
  "tenants.count",
  "tenants.keyCount",
  "monitoring.nodes.count",
  "monitoring.runtimes.count",
] as const;

export type PluralTranslationKey = (typeof PLURAL_KEYS)[number];
