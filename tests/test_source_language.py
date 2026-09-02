from __future__ import annotations

import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {
    ".conf",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
SOURCE_BASENAMES = {"Dockerfile", "Makefile", "requirements.lock"}
ALLOWED_CHINESE_PATHS = {
    pathlib.Path("scripts/test-adversarial.py"),
}
SKIP_DIRECTORIES = {
    ".git",
    ".sandbox",
    ".planning",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
}


class SourceLanguageTests(unittest.TestCase):
    def test_sources_use_english_outside_localization_and_unicode_fixtures(self) -> None:
        offenders: list[str] = []
        for path in ROOT.rglob("*"):
            if (
                SKIP_DIRECTORIES.intersection(path.relative_to(ROOT).parts[:-1])
                or path.suffix not in SOURCE_SUFFIXES
                and path.name not in SOURCE_BASENAMES
                or path.relative_to(ROOT) in ALLOWED_CHINESE_PATHS
                or path.is_relative_to(ROOT / "console/src/i18n/locales")
                or not path.is_file()
            ):
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"[\u3400-\u9fff]", text):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
