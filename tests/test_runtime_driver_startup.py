"""``SANDBOX_RUNTIME_DRIVER`` accepts only ``gvisor``; anything else exits at startup.

README says so, and a mutation that let the setting accept ``runc`` turned no
test red: the check is a module-level ``SystemExit`` in ``control_plane/core.py``
that only a fresh interpreter can observe. This starts one per value.
"""

from __future__ import annotations

import subprocess
import sys
import unittest

from tests.test_login_paths import ROOT, control_plane_environment


def start_with_driver(value: str | None) -> subprocess.CompletedProcess:
    # The volume role constructs no Kubernetes client, so the positive cases
    # reach the end of the module instead of failing on cluster settings.
    environment = control_plane_environment()
    environment["SANDBOX_CONTROL_PLANE_ROLE"] = "volume"
    environment.pop("SANDBOX_RUNTIME_DRIVER", None)
    if value is not None:
        environment["SANDBOX_RUNTIME_DRIVER"] = value
    return subprocess.run(
        [sys.executable, "-c", "from control_plane import core; print('started', core.SANDBOX_RUNTIME_DRIVER)"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


class RuntimeDriverStartupTests(unittest.TestCase):
    def test_any_driver_other_than_gvisor_exits_with_the_reason(self) -> None:
        for value in ("runc", "kata", "docker", "gvisor2", ""):
            with self.subTest(driver=value):
                result = start_with_driver(value)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("started", result.stdout)
                self.assertIn("SANDBOX_RUNTIME_DRIVER", result.stderr)
                self.assertIn("gvisor", result.stderr)

    def test_gvisor_in_any_case_and_the_default_start(self) -> None:
        for value in ("gvisor", " GVisor ", None):
            with self.subTest(driver=value):
                result = start_with_driver(value)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("started gvisor", result.stdout)


if __name__ == "__main__":
    unittest.main()
