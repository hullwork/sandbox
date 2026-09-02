"""The published wheel claims one import name, and the config can produce no other.

Two guards, because they fail at different times. `WheelSurfaceScriptTests`
exercises the check that CI runs against a real built wheel, using synthetic
wheels so the assertion is testable without a build backend.
`PackagingSurfaceConfigTests` reads `pyproject.toml` and refuses any file
selection whose result this test cannot derive - that is what turns a new
`force-include` or a repository-root module added to `packages` red here,
before anyone builds anything.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import tempfile
import tomllib
import unittest
import zipfile

import sandbox_platform

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_wheel_surface", ROOT / "scripts/check-wheel-surface.py"
)
assert SPEC and SPEC.loader
check_wheel_surface = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_wheel_surface)

PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
WHEEL_TARGET = PROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]

# Hatchling keys that add or move files in the wheel. `packages` is the one this
# project uses; any other appearing here means the top-level surface is no longer
# derivable from `packages` alone, so the reasoning below stops holding.
FILE_SELECTION_KEYS = frozenset(
    {"artifacts", "exclude", "force-include", "include", "only-include", "packages", "sources"}
)


def build_wheel(directory: pathlib.Path, members: dict[str, str]) -> pathlib.Path:
    wheel = directory / "synthetic-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("synthetic-0.0.0.dist-info/METADATA", "Name: synthetic\n")
        for name, content in members.items():
            archive.writestr(name, content)
    return wheel


class WheelSurfaceScriptTests(unittest.TestCase):
    def check(self, members: dict[str, str]) -> tuple[int, str]:
        """Return the script's exit status and what it reported, both captured."""
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            wheel = build_wheel(pathlib.Path(directory), members)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = check_wheel_surface.main([str(wheel)])
        return status, out.getvalue() + err.getvalue()

    def test_a_wheel_shipping_only_the_package_is_accepted(self) -> None:
        status, report = self.check({"sandbox_platform/__init__.py": ""})
        self.assertEqual(status, 0)
        self.assertIn("sandbox_platform", report)

    def test_a_flat_module_beside_the_package_is_refused(self) -> None:
        # The failure this exists for: a module at the wheel root installs into
        # site-packages under a name any other distribution may also claim.
        status, report = self.check(
            {"sandbox_platform/__init__.py": "", "telemetry.py": ""}
        )
        self.assertEqual(status, 1)
        self.assertIn("telemetry.py", report)

    def test_a_path_configuration_file_is_refused(self) -> None:
        status, report = self.check(
            {"sandbox_platform/__init__.py": "", "sandbox.pth": "/opt\n"}
        )
        self.assertEqual(status, 1)
        self.assertIn("sandbox.pth", report)

    def test_a_data_payload_is_refused(self) -> None:
        status, report = self.check(
            {
                "sandbox_platform/__init__.py": "",
                "synthetic-0.0.0.data/purelib/telemetry.py": "",
            }
        )
        self.assertEqual(status, 1)
        self.assertIn("synthetic-0.0.0.data", report)

    def test_a_wheel_without_the_package_is_refused(self) -> None:
        status, report = self.check({"telemetry.py": ""})
        self.assertEqual(status, 1)
        self.assertIn("missing ['sandbox_platform']", report)

    def test_a_wheel_of_nothing_but_metadata_is_refused(self) -> None:
        # The case a subset check would pass: no unexpected name is present, so
        # `entries <= ALLOWED_TOP_LEVEL` holds and an empty release is reported
        # as good. Only equality catches it, and this is the reading that proves
        # the difference - without it the two forms score identically.
        status, report = self.check({})
        self.assertEqual(status, 1)
        self.assertIn("missing ['sandbox_platform']", report)

    def test_the_allowed_surface_is_the_single_published_package(self) -> None:
        self.assertEqual(set(check_wheel_surface.ALLOWED_TOP_LEVEL), {"sandbox_platform"})


class PackagingSurfaceConfigTests(unittest.TestCase):
    def test_the_wheel_ships_exactly_the_published_package(self) -> None:
        self.assertEqual(WHEEL_TARGET["packages"], ["sandbox_platform"])

    def test_the_wheel_target_selects_files_no_other_way(self) -> None:
        # Adding any of these keys may put a repository-root module into the
        # wheel, so it has to be reviewed against ALLOWED_TOP_LEVEL by hand.
        self.assertEqual(FILE_SELECTION_KEYS.intersection(WHEEL_TARGET), {"packages"})

    def test_every_console_script_lives_inside_the_package(self) -> None:
        for name, target in PROJECT["project"]["scripts"].items():
            self.assertTrue(target.startswith("sandbox_platform."), f"{name} -> {target}")

    def test_the_runtime_version_matches_the_distribution_version(self) -> None:
        # The release workflow pins the tag to project.version only. Without
        # this, `sandbox-mcp --version` and the MCP server info could report a
        # version no released artifact ever carried.
        self.assertEqual(sandbox_platform.__version__, PROJECT["project"]["version"])

    def test_no_dependency_is_a_direct_url(self) -> None:
        # PyPI refuses an upload whose metadata names a direct URL, so one
        # `package @ git+https://...` requirement makes this project
        # unpublishable - and unusable under a `--require-hashes` lock file.
        requirements = list(PROJECT["project"].get("dependencies", []))
        for extra in PROJECT["project"].get("optional-dependencies", {}).values():
            requirements.extend(extra)
        for requirement in requirements:
            self.assertNotIn("@", requirement, requirement)


if __name__ == "__main__":
    unittest.main()
