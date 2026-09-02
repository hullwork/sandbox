from __future__ import annotations

import os
import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV_SCRIPT = ROOT / "scripts/e2e-env.sh"


class E2eEnvironmentTests(unittest.TestCase):
    def resolve(self, **values: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        for name in (
            "SANDBOX_KUBE_CONTEXT",
            "SANDBOX_CONTROL_PLANE_URL",
        ):
            environment.pop(name, None)
        environment.update(values)
        return subprocess.run(
            [
                "bash",
                "-c",
                (
                    f"source {ENV_SCRIPT!s}; resolve_e2e_environment || exit $?; "
                    "printf '%s|%s' "
                    '"$SANDBOX_KUBE_CONTEXT" "$SANDBOX_CONTROL_PLANE_URL"'
                ),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )

    def test_defaults_have_one_cluster_and_control_plane_target(self) -> None:
        result = self.resolve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "sandbox-local|http://127.0.0.1:18080",
        )

    def test_canonical_values_override_defaults(self) -> None:
        result = self.resolve(
            SANDBOX_KUBE_CONTEXT="sandbox-local",
            SANDBOX_CONTROL_PLANE_URL="http://127.0.0.1:28080",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "sandbox-local|http://127.0.0.1:28080",
        )

    def test_every_shell_scenario_sources_the_shared_contract(self) -> None:
        for relative in (
            "scripts/run-all-e2e.sh",
            "scripts/verify-network-policy.sh",
            "scripts/test.sh",
            "scripts/test-object-store.sh",
            "scripts/test-restart.sh",
        ):
            with self.subTest(path=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn('source "${SCRIPT_DIR}/e2e-env.sh"', source)
                self.assertIn("resolve_e2e_environment", source)

    def test_network_policy_reuses_its_workspace(self) -> None:
        source = (
            ROOT / "scripts/verify-network-policy.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'POLICY_SESSION_ID="policy-e2e-network-policy"',
            source,
        )
        self.assertNotIn('"/v1/workspaces/${WORKSPACE_ID}?purge=true"', source)


if __name__ == "__main__":
    unittest.main()
