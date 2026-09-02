import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const localeDirectory = join(
  dirname(fileURLToPath(import.meta.url)),
  "../src/i18n",
);
const configSource = readFileSync(join(localeDirectory, "config.ts"), "utf8");
const localeList = configSource.match(/export const LOCALES = \[([\s\S]*?)\] as const/);
const pluralList = configSource.match(/export const PLURAL_KEYS = \[([\s\S]*?)\] as const/);

if (!localeList || !pluralList) {
  throw new Error("Unable to parse LOCALES and PLURAL_KEYS from src/i18n/config.ts");
}

const locales = [...localeList[1].matchAll(/"([^"]+)"/g)].map((match) => match[1]);
const pluralKeys = [...pluralList[1].matchAll(/"([^"]+)"/g)].map((match) => match[1]);
const catalogs = new Map();

if (locales.length === 0) {
  throw new Error("LOCALES must contain at least one locale");
}

if (new Set(locales).size !== locales.length) {
  throw new Error("LOCALES contains duplicate entries");
}

function parseCatalog(path) {
  const source = readFileSync(path, "utf8");
  const entries = new Map();
  const pattern = /^\s*"([^"]+)":\s*"((?:\\.|[^"\\])*)",?$/gm;
  let match;
  while ((match = pattern.exec(source))) {
    if (entries.has(match[1])) {
      throw new Error(`Duplicate translation key ${match[1]} in ${path}`);
    }
    entries.set(match[1], match[2]);
  }
  return entries;
}

for (const locale of locales) {
  catalogs.set(
    locale,
    parseCatalog(join(localeDirectory, "locales", `${locale}.ts`)),
  );
}

const english = catalogs.get("en");
const failures = [];

function sourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      return entry.name === "locales" ? [] : sourceFiles(path);
    }
    return /\.(ts|tsx|css)$/.test(entry.name) ? [path] : [];
  });
}

for (const path of sourceFiles(join(localeDirectory, ".."))) {
  const lines = readFileSync(path, "utf8").split(/\r?\n/);
  lines.forEach((line, index) => {
    const trimmed = line.trimStart();
    const isComment = trimmed.startsWith("//")
      || trimmed.startsWith("/*")
      || trimmed.startsWith("*")
      || trimmed.startsWith("{/*");
    if (isComment && /\p{Script=Han}/u.test(line)) {
      failures.push(`non-English source comment at ${path}:${index + 1}`);
    }
  });
}

if (!locales.includes("en")) {
  failures.push("locale list must include the English source locale");
}

for (const pluralKey of pluralKeys) {
  for (const suffix of ["one", "other"]) {
    if (!english.has(`${pluralKey}.${suffix}`)) {
      failures.push(`en: missing plural key ${pluralKey}.${suffix}`);
    }
  }
}

for (const locale of locales.filter((locale) => locale !== "en")) {
  const catalog = catalogs.get(locale);
  const expected = new Set(english.keys());
  const actual = new Set(catalog.keys());

  for (const key of expected) {
    if (!actual.has(key)) {
      failures.push(`${locale}: missing ${key}`);
    }
    const expectedPlaceholders = new Set([
      ...english.get(key).matchAll(/\{(\w+)\}/g),
    ].map((match) => match[1]));
    const actualPlaceholders = new Set([
      ...catalog.get(key).matchAll(/\{(\w+)\}/g),
    ].map((match) => match[1]));

    for (const placeholder of expectedPlaceholders) {
      if (!actualPlaceholders.has(placeholder)) {
        failures.push(`${locale}: ${key} is missing {${placeholder}}`);
      }
    }
    for (const placeholder of actualPlaceholders) {
      if (!expectedPlaceholders.has(placeholder)) {
        failures.push(`${locale}: ${key} has unexpected {${placeholder}}`);
      }
    }
  }

  for (const pluralKey of pluralKeys) {
    for (const suffix of ["one", "other"]) {
      if (!actual.has(`${pluralKey}.${suffix}`)) {
        failures.push(`${locale}: missing plural key ${pluralKey}.${suffix}`);
      }
    }
  }

  for (const key of actual) {
    if (!expected.has(key)) {
      failures.push(`${locale}: unexpected ${key}`);
    }
  }
}

if (failures.length > 0) {
  console.error(failures.join("\n"));
  process.exitCode = 1;
} else {
  console.log(`i18n catalogs passed (${english.size} keys)`);
}
