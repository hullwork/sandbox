"""The reserved management tenant is created wherever it is first needed.

The unscoped management identity has no tenant of its own, but the control
plane now needs a row for it: capability_epoch refuses to answer for a subject
it has no record of, because minting a ticket for an unknown subject is the
forgery that epoch exists to prevent.

🔴 The row therefore has to appear on *every* path that needs it, not just the
one someone happened to hit first. It was originally created only while
registering a workspace, so an operator whose first management call was
``POST /v1/sandboxes`` got ``unknown tenant: management`` -- which reads as a
misconfiguration, sends them looking at their tenant list, and is nowhere near
the actual cause.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from tests.test_login_paths import ROOT, control_plane_environment

PROBE = textwrap.dedent(
    """
    import json

    from control_plane import kube
    class FakeKube:
        def __init__(self):
            pass

        def list(self, namespace, plural, *, label_selector=None):
            return []

    kube.KubeClient = FakeKube

    from control_plane import api
    from control_plane import core as control_plane
    #The schema is created by the serving entrypoint, which this probe does
    #not run: it exercises one handler method, not a whole process.
    control_plane.STORE.ensure_schema()

    out = {}
    out["seeded"] = control_plane.STORE.get_tenant("management") is not None

    handler = object.__new__(api.ApiHandler)
    handler.tenant_id = None
    handler.api_key = None
    out["limit"] = handler.tenant_runtime_limit()
    out["created"] = control_plane.STORE.get_tenant("management") is not None

    #Second call must not depend on the first having run.
    handler2 = object.__new__(api.ApiHandler)
    handler2.tenant_id = None
    handler2.api_key = None
    out["limit_again"] = handler2.tenant_runtime_limit()
    print(json.dumps(out))
    """
)


def run() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        result = subprocess.run(
            [sys.executable, "-c", PROBE],
            cwd=ROOT,
            env=control_plane_environment(
                SANDBOX_STORE_BACKEND="sqlite",
                SANDBOX_STORE_PATH=os.path.join(directory, "control-plane.db"),
            ),
            capture_output=True,
            text=True,
            timeout=120,
        )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class ManagementTenantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.probe = run()

    def test_the_row_is_absent_before_anything_asks_for_it(self) -> None:
        #Without this the assertion below would also pass on a schema that
        #seeds the row, and the on-demand creation it is meant to cover would
        #not be exercised at all.
        self.assertFalse(self.probe["seeded"])

    def test_a_runtime_limit_lookup_creates_it_rather_than_404ing(self) -> None:
        self.assertIsInstance(self.probe["limit"], int)
        self.assertGreater(self.probe["limit"], 0)
        self.assertTrue(self.probe["created"])

    def test_the_lookup_is_repeatable(self) -> None:
        self.assertEqual(self.probe["limit_again"], self.probe["limit"])


if __name__ == "__main__":
    unittest.main()
