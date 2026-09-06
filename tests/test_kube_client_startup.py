"""A Control Plane that cannot reach its service account must say what to do.

``core.py`` states the rule for the configuration block: SystemExit rather than
KeyError, because "what the container wants is an instruction to follow, not a
stack trace". ``KubeClient`` is constructed at import time from the same kind of
environment, so it is held to the same rule. Both inputs are missing together in
the two situations that actually occur - started outside a cluster, and
``automountServiceAccountToken: false`` - and a traceback names neither.
"""

from __future__ import annotations

import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from control_plane import kube  # noqa: E402


class KubeClientStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = {
            name: os.environ.get(name)
            for name in (
                "KUBERNETES_SERVICE_HOST",
                "KUBERNETES_TOKEN_FILE",
                "KUBERNETES_CA_FILE",
            )
        }

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_outside_a_cluster_it_names_the_cause_not_the_variable(self) -> None:
        os.environ.pop("KUBERNETES_SERVICE_HOST", None)
        with self.assertRaises(SystemExit) as caught:
            kube.KubeClient()
        message = str(caught.exception)
        self.assertIn("KUBERNETES_SERVICE_HOST", message)
        self.assertIn("not running inside a Kubernetes Pod", message)

    def test_an_unreadable_service_account_token_names_the_file(self) -> None:
        os.environ["KUBERNETES_SERVICE_HOST"] = "10.0.0.1"
        os.environ["KUBERNETES_TOKEN_FILE"] = "/nonexistent/sandbox-token"
        os.environ["KUBERNETES_CA_FILE"] = "/nonexistent/sandbox-ca.crt"
        with self.assertRaises(SystemExit) as caught:
            kube.KubeClient()
        message = str(caught.exception)
        self.assertIn("/nonexistent/sandbox-token", message)
        self.assertIn("automountServiceAccountToken", message)


if __name__ == "__main__":
    unittest.main()
