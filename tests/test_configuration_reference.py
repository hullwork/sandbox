"""Every environment variable the code reads is in the configuration reference.

`docs/CONFIGURATION.md` calls itself a reference. The 2026-09-02 review found
that the code read 121 environment variables and the document named 58 of
them, the three `OBJECT_STORE_*` values that startup refuses to run without
among the missing. A reference that lists half of the settings is worse than
none, because the reader stops looking.

The scan is deliberately dumb: literal names passed to `os.getenv`,
`os.environ[...]`, `os.environ.get(...)` and the `*_env("NAME")` helpers in
non-test, non-script Python. `control_plane/grafana_proxy.py` reads through a
helper that prepends `ENV_PREFIX`, so its names are expanded the same way.
Names may be documented in `docs/CONFIGURATION.md` or, for the values that
are capacity limits first and settings second, `docs/SYSTEM_SPECIFICATIONS.md`.
"""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", "node_modules", "tests", "scripts", "console", "dist"}
READ_PATTERN = re.compile(
    r"""(?:environ(?:\.get|\.setdefault|\.pop)?\s*[\[(]\s*"""
    r"""|getenv\s*\(\s*"""
    r"""|\w*_env\s*\(\s*)["']([A-Z][A-Z0-9_]+)["']"""
)
PREFIXED_HELPER = re.compile(r"""(?<![\w.])_env\s*\(\s*["']([A-Z][A-Z0-9_]+)["']""")
DOCUMENTED_IN = ("docs/CONFIGURATION.md", "docs/SYSTEM_SPECIFICATIONS.md")
DOC_TOKEN = re.compile(r"`([A-Z][A-Z0-9_]+)`")


def _env_prefix(text: str) -> str:
    match = re.search(r'^ENV_PREFIX\s*=\s*"([A-Z0-9_]*)"', text, re.MULTILINE)
    return match.group(1) if match else ""


def environment_names_read_by_code() -> dict[str, set[str]]:
    """Map each literal environment name to the files reading it."""
    found: dict[str, set[str]] = {}
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if SKIP_PARTS.intersection(relative.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        prefix = _env_prefix(text)
        prefixed = set(PREFIXED_HELPER.findall(text)) if prefix else set()
        for name in READ_PATTERN.findall(text):
            if name in prefixed:
                name = prefix + name
            found.setdefault(name, set()).add(str(relative))
    return found


def documented_names() -> set[str]:
    names: set[str] = set()
    for relative in DOCUMENTED_IN:
        names.update(DOC_TOKEN.findall((ROOT / relative).read_text(encoding="utf-8")))
    return names


class ConfigurationReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.read = environment_names_read_by_code()

    def test_the_scan_still_sees_the_code(self) -> None:
        # A regex that silently matched nothing would make the next test pass
        # for the wrong reason. Pin a floor and three names from three roles.
        self.assertGreaterEqual(len(self.read), 100, sorted(self.read))
        for name in (
            "OBJECT_STORE_ENDPOINT",       # Control Plane, required at startup
            "SANDBOX_MAX_SHELL_SESSIONS",  # Runtime
            "FILE_SERVICE_PORT",           # file-service
            "SANDBOX_GRAFANA_URL",         # read through the prefixed helper
        ):
            self.assertIn(name, self.read)

    def test_every_name_the_code_reads_is_documented(self) -> None:
        documented = documented_names()
        missing = sorted(name for name in self.read if name not in documented)
        detail = "\n".join(
            f"{name}  ({', '.join(sorted(self.read[name]))})" for name in missing
        )
        self.assertEqual(
            missing, [],
            f"{len(missing)} environment variable(s) read by the code are in "
            f"neither {' nor '.join(DOCUMENTED_IN)}:\n{detail}",
        )


if __name__ == "__main__":
    unittest.main()
