"""The control_plane image must ship every module the control_plane imports.

This guard exists because the omission is invisible until a container starts:
the build succeeds, the tests pass from the repository root where every module
resolves, and the failure surfaces as a CrashLoop with ModuleNotFoundError on a
cluster. The Dockerfile lists sources one by one and says so in a comment,
which makes "someone added a module and did not touch the COPY line" the
expected way for this to break rather than an unlikely one -- it has already
happened twice.

The criterion is the *semantics* of the COPY instruction, not the literal line,
so a legal rewrite (line continuations, a different uid, copying a whole
directory) must not turn this red. A guard that goes red on correct code gets
switched off, and then it does not catch the real omission either.
"""
from __future__ import annotations

import pathlib
import unittest

from tests.contract_helpers import copy_sources

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SANDBOX_CONTROL_PLANE_DIR = REPO_ROOT / "control_plane"
DOCKERFILE = SANDBOX_CONTROL_PLANE_DIR / "Dockerfile"


class ControlPlaneImageContentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = copy_sources(DOCKERFILE.read_text(encoding="utf-8"))

    def test_the_scan_finds_something(self) -> None:
        #Without this, a broken parser would make every assertion below pass
        #over an empty set, and the suite would report a guard that is not
        #looking at anything.
        self.assertTrue(self.sources, "no COPY sources parsed out of the Dockerfile")

    def test_the_complete_control_plane_package_is_copied(self) -> None:
        self.assertTrue(
            any(source.rstrip("/") == "control_plane" for source in self.sources),
            "the image must copy the complete control_plane package so new drivers "
            "and adapters cannot be omitted",
        )

    def test_entrypoint_runs_from_the_package_parent(self) -> None:
        instructions = [
            line.strip()
            for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        entrypoint = max(
            index for index, line in enumerate(instructions)
            if line.startswith("ENTRYPOINT ")
        )
        workdirs = [
            line for line in instructions[:entrypoint]
            if line.startswith("WORKDIR ")
        ]
        self.assertTrue(workdirs, "the final image has no working directory")
        self.assertEqual(
            workdirs[-1],
            "WORKDIR /app",
            "python -m control_plane.server must start with /app on sys.path",
        )

    def test_automation_uses_real_control_plane_paths_and_valid_image_tags(self) -> None:
        ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        release = (REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        dependabot = (REPO_ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        for source in (ci, release):
            self.assertIn("control_plane/Dockerfile", source)
            self.assertNotIn("control plane/Dockerfile", source)
        self.assertIn("tags: sandbox-${{ matrix.name }}:ci", ci)
        self.assertIn("directory: /control_plane", dependabot)

    def test_the_build_time_smoke_import_names_every_copied_module(self) -> None:
        #The COPY list and the smoke import are two records of the same fact.
        #A module copied but not imported at build time is only checked by
        #reality; a module imported but not copied fails the build, which is
        #the direction we want.
        text = DOCKERFILE.read_text(encoding="utf-8")
        smoke = [
            line
            for line in text.splitlines()
            if 'python3 -c "import control_plane.core' in line
        ]
        self.assertEqual(len(smoke), 1, "expected exactly one build-time smoke import")
        for source in sorted(SANDBOX_CONTROL_PLANE_DIR.glob("*.py")):
            if source.name == "__init__.py":
                continue
            name = f"control_plane.{source.stem}"
            with self.subTest(module=name):
                self.assertIn(
                    name,
                    smoke[0],
                    f"{name} is not named in the build-time smoke import",
                )


if __name__ == "__main__":
    unittest.main()
