"""Every locked package outside the permissive set is named in the notices file.

The 2026-09-02 review found `psycopg` and `psycopg-binary` (LGPL-3.0-only,
the latter bundling libpq and OpenSSL) pinned in the Control Plane lockfile
and imported at image build, while `THIRD_PARTY_NOTICES.md` had one generic
row. LGPL section 4 asks distributors to give prominent notice; a table that
does not name the package cannot. The 2026-09-01 review had already reported
it. Nothing guarded the table, so nothing changed.

License facts come from the installed distribution's metadata when the
package is in the test environment, and from `KNOWN_LICENSES` when it is not
(the optional database drivers are not installed for the unit tests). A
package in neither place fails loudly: an unknown license is not a permissive
one.
"""
from __future__ import annotations

import importlib.metadata
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCKFILE = ROOT / "control_plane/requirements.lock"
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==")

# Licenses of locked packages that the unit-test environment does not install.
# Add a package here only after reading its metadata; the value is the SPDX
# expression, so a copyleft entry lands in the "must be in the table" set.
KNOWN_LICENSES = {
    "psycopg": "LGPL-3.0-only",
    "psycopg-binary": "LGPL-3.0-only",
    "typing-extensions": "PSF-2.0",
    "pymysql": "MIT",
}
PERMISSIVE = re.compile(
    r"\b(MIT|BSD|Apache|ISC|PSF|Python Software Foundation|MPL|Mozilla)\b",
    re.IGNORECASE,
)
COPYLEFT = re.compile(r"GPL", re.IGNORECASE)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_packages() -> list[str]:
    names = []
    for line in LOCKFILE.read_text(encoding="utf-8").splitlines():
        match = PIN.match(line.strip())
        if match:
            names.append(match.group(1))
    return names


def license_of(name: str) -> str:
    try:
        meta = importlib.metadata.distribution(name).metadata
    except importlib.metadata.PackageNotFoundError:
        return KNOWN_LICENSES.get(_normalize(name), "")
    parts = [meta.get("License-Expression") or "", meta.get("License") or ""]
    parts += [c for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]
    return " | ".join(p for p in parts if p)


def is_permissive(license_text: str) -> bool:
    return bool(PERMISSIVE.search(license_text)) and not COPYLEFT.search(license_text)


def notice_table_rows() -> list[str]:
    return [
        line for line in NOTICES.read_text(encoding="utf-8").splitlines()
        if line.startswith("|") and not re.fullmatch(r"\|[ \-:|]+\|?", line)
    ]


class ThirdPartyNoticesTests(unittest.TestCase):
    def test_the_lockfile_is_still_being_read(self) -> None:
        names = locked_packages()
        self.assertGreaterEqual(len(names), 8, names)
        self.assertIn("boto3", names)

    def test_every_locked_package_has_a_known_license(self) -> None:
        unknown = [n for n in locked_packages() if not license_of(n)]
        self.assertEqual(
            unknown, [],
            "no license metadata and no KNOWN_LICENSES entry; read the "
            "package metadata and add it, do not assume permissive",
        )

    def test_non_permissive_packages_are_named_in_the_notices_table(self) -> None:
        rows = " ".join(notice_table_rows()).lower()
        missing = []
        for name in locked_packages():
            license_text = license_of(name)
            if license_text and not is_permissive(license_text):
                if f"`{name.lower()}`" not in rows:
                    missing.append(f"{name} ({license_text})")
        self.assertEqual(
            missing, [],
            f"locked packages outside the permissive set with no row in {NOTICES.name}",
        )

    def test_the_permissive_classifier_is_not_fooled_by_lesser_gpl(self) -> None:
        # "LGPL-3.0-only" contains no permissive token, but a dual-licensed
        # string could: the copyleft marker must win.
        self.assertFalse(is_permissive("LGPL-3.0-only"))
        self.assertFalse(is_permissive("MIT OR LGPL-3.0-only"))
        self.assertTrue(is_permissive("Dual License | License :: OSI Approved :: BSD License"))
        self.assertFalse(is_permissive(""))


if __name__ == "__main__":
    unittest.main()
