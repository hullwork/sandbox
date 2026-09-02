import type { Locale } from "./i18n";
import { en } from "./i18n/locales/en";
import { zhCN } from "./i18n/locales/zh-CN";

/**
 * Control Plane timestamp helpers.
 *
 * Control Plane runtime timestamps are strings containing Unix seconds (taken directly
 * from Pod annotations) and may be null. Keeping parsing and formatting here avoids
 * duplicating edge-case handling in every table.
 */

function toSeconds(value: string | null | undefined): number | null {
  if (!value) {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function formatUnix(
  value: string | null | undefined,
  locale: Locale = "en",
): string {
  const seconds = toSeconds(value);
  if (seconds === null) {
    return "—";
  }
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "medium",
    hour12: false,
  }).format(new Date(seconds * 1000));
}

function relativeMessage(
  locale: Locale,
  unit: "seconds" | "minutes" | "hours" | "compoundHours",
  count: number,
  past: boolean,
  minutes = 0,
): string {
  const messages = {
    en: {
      secondsPast: en["relative.secondsPast"],
      secondsFuture: en["relative.secondsFuture"],
      minutesPast: en["relative.minutesPast"],
      minutesFuture: en["relative.minutesFuture"],
      hoursPast: en["relative.hoursPast"],
      hoursFuture: en["relative.hoursFuture"],
      compoundHoursPast: en["relative.compoundHoursPast"],
      compoundHoursFuture: en["relative.compoundHoursFuture"],
    },
    "zh-CN": {
      secondsPast: zhCN["relative.secondsPast"],
      secondsFuture: zhCN["relative.secondsFuture"],
      minutesPast: zhCN["relative.minutesPast"],
      minutesFuture: zhCN["relative.minutesFuture"],
      hoursPast: zhCN["relative.hoursPast"],
      hoursFuture: zhCN["relative.hoursFuture"],
      compoundHoursPast: zhCN["relative.compoundHoursPast"],
      compoundHoursFuture: zhCN["relative.compoundHoursFuture"],
    },
  }[locale];
  const template = messages[`${unit}${past ? "Past" : "Future"}`];
  return template
    .replace("{count}", String(count))
    .replace("{hours}", String(count))
    .replace("{minutes}", String(minutes));
}

/**
 * Formats a directional relative time.
 *
 * Reclaim countdowns depend on the direction, so this deliberately distinguishes
 * past from future instead of returning an absolute duration.
 */
export function formatRelative(
  value: string | null | undefined,
  now: number = Date.now() / 1000,
  locale: Locale = "en",
): string {
  const seconds = toSeconds(value);
  if (seconds === null) {
    return "—";
  }
  const delta = seconds - now;
  // Round before bucketing. `now` has fractions, so exactly 3600 seconds can
  // otherwise fall into the minute bucket and render as “59 minutes”, not “1 hour”.
  const absolute = Math.round(Math.abs(delta));
  const past = delta < 0;

  if (absolute < 60) {
    return relativeMessage(locale, "seconds", Math.floor(absolute), past);
  }
  if (absolute < 3600) {
    return relativeMessage(locale, "minutes", Math.floor(absolute / 60), past);
  }
  if (absolute < 86400) {
    const hours = Math.floor(absolute / 3600);
    const minutes = Math.floor((absolute % 3600) / 60);
    return minutes
      ? relativeMessage(locale, "compoundHours", hours, past, minutes)
      : relativeMessage(locale, "hours", hours, past);
  }

  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "always" });
  const days = Math.floor(absolute / 86400);
  return formatter.format(past ? -days : days, "day");
}

/** Returns whether an expiry timestamp is already in the past. */
export function isPast(
  value: string | null | undefined,
  now: number = Date.now() / 1000,
): boolean {
  const seconds = toSeconds(value);
  return seconds !== null && seconds < now;
}

/**
 * Parses control-plane database timestamps.
 *
 * SQLite's CURRENT_TIMESTAMP returns values such as `2026-08-15 03:04:05`: UTC
 * without a zone marker. Browsers parse that shape as local time, which is eight
 * hours wrong in Shanghai and visually plausible enough to go unnoticed. Append Z
 * before parsing; PostgreSQL's ISO strings already contain an offset.
 */
function parseDbTime(value: string | null | undefined): Date | null {
  if (!value) {
    return null;
  }
  const naive = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?$/.test(value);
  const date = new Date(naive ? `${value.replace(" ", "T")}Z` : value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDbTime(
  value: string | null | undefined,
  locale: Locale = "en",
): string {
  if (!value) {
    return "—";
  }
  const date = parseDbTime(value);
  if (date === null) {
    // Preserve the raw value. If the database format changes, the original string
    // identifies the changing layer faster than a generic invalid-date label.
    return value;
  }
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
    hour12: false,
  }).format(date);
}

/**
 * Formats a control-plane timestamp as an approximate relative time.
 *
 * Control Plane throttles `last_used_at` updates to five minutes. Rendering an exact clock
 * time would overpromise precision; relative time is honest at that granularity.
 */
export function formatDbRelative(
  value: string | null | undefined,
  locale: Locale = "en",
): string {
  const parsed = parseDbTime(value);
  if (parsed === null) {
    return "—";
  }
  return formatRelative(String(Math.floor(parsed.getTime() / 1000)), undefined, locale);
}
