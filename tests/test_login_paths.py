"""Which credentials can actually sign in, checked against a running Control Plane.

🔴 Every case here bypasses the Console entirely and talks to the HTTP API. A
switch that only removes a form from the front end has not disabled anything;
this is the most commonly lost control in this whole area, and the only way to
know it holds is to knock on the API door directly.

The startup cases assert the process **refuses to start**. Silently degrading to
"nobody can sign in" or "the escape hatch is still open" are the two failure
directions this configuration has, and both are invisible without a hard exit.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]

PROBE = textwrap.dedent(
    """
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from control_plane import kube
    class FakeKube:
        def __init__(self):
            pass

        def list(self, namespace, plural, *, label_selector=None):
            return []

    kube.KubeClient = FakeKube

    from control_plane import api
    from control_plane import core as control_plane
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.ApiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    results = {}

    def call(name, path, token=None, headers=None):
        request = urllib.request.Request(f"{base}{path}", method="GET")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        results[name] = {
            "status": status,
            "body": json.loads(raw) if raw else None,
        }

    try:
        results["local_login_enabled"] = control_plane.LOCAL_LOGIN_ENABLED
        results["store_configured"] = control_plane.STORE is not None
        if control_plane.STORE is not None:
            control_plane.STORE.ensure_schema()
            control_plane.STORE.create_tenant(
                "acme", "Acme", max_workspaces=2, max_runtimes=2
            )
            issued, _ = control_plane.STORE.issue_api_key("acme", "an integrator")
            call("api_key", "/v1/whoami", token=issued)
        results["oidc_configured"] = control_plane.OIDC_CONFIG is not None
        results["control_plane_token_is_loaded"] = bool(control_plane.SANDBOX_CONTROL_PLANE_TOKEN)
        call("methods", "/v1/auth/methods")
        call("anonymous", "/v1/whoami")
        call("static_token", "/v1/whoami", token=STATIC_TOKEN)
        call("oidc_login", "/v1/auth/oidc/login")
    finally:
        server.shutdown()
        server.server_close()
    print(json.dumps(results))
    """
)

STATIC_TOKEN = "test-break-glass-token"

BASE_ENVIRONMENT = {
    "SANDBOX_CONTROL_PLANE_TOKEN": STATIC_TOKEN,
    "SIGNING_KEY": "0" * 32,
    "WORKSPACE_ID_KEY": "1" * 32,
    "VOLUME_AGENT_URL": "http://127.0.0.1:1",
    "VOLUME_AGENT_TOKEN": "test-volume-token",
    "OBJECT_STORE_ENDPOINT": "http://127.0.0.1:1",
    "OBJECT_STORE_ACCESS_KEY": "test-access",
    "OBJECT_STORE_SECRET_KEY": "test-secret",
}

OIDC_ENVIRONMENT = {
    "SANDBOX_CONTROL_PLANE_OIDC_ISSUER": "https://issuer.invalid/realms/sandbox",
    "SANDBOX_CONTROL_PLANE_OIDC_CLIENT_ID": "sandbox-console",
    "SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE": "sandbox-control-plane",
    "SANDBOX_CONTROL_PLANE_OIDC_REDIRECT_URL": "https://sandbox.invalid/v1/auth/oidc/callback",
    "SANDBOX_CONTROL_PLANE_OIDC_ADMIN_GROUPS": "platform-operators",
}


def control_plane_environment(**overrides: str) -> dict[str, str]:
    environment = {
        **os.environ,
        **BASE_ENVIRONMENT,
        **overrides,
        "PYTHONPATH": str(ROOT),
    }
    environment.pop("SANDBOX_CONTROL_PLANE_ROLE", None)
    for name in list(environment):
        if name.startswith("SANDBOX_CONTROL_PLANE_OIDC_") and name not in overrides:
            environment.pop(name)
    return environment


def run_probe(*, store: bool = False, **overrides: str) -> dict:
    source = f"STATIC_TOKEN = {STATIC_TOKEN!r}\n" + PROBE
    with tempfile.TemporaryDirectory() as directory:
        if store:
            overrides = {
                **overrides,
                "SANDBOX_STORE_BACKEND": "sqlite",
                "SANDBOX_STORE_PATH": os.path.join(directory, "control-plane.db"),
            }
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=ROOT,
            env=control_plane_environment(**overrides),
            capture_output=True,
            text=True,
            timeout=120,
        )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


def run_startup(**overrides: str) -> subprocess.CompletedProcess:
    # The Kubernetes client is constructed at import time and wants in-cluster
    # environment; the configuration gate runs well before it, which is exactly
    # what these cases are about.
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from control_plane import kube; kube.KubeClient = lambda *a, **k: None; from control_plane import core as control_plane",
        ],
        cwd=ROOT,
        env=control_plane_environment(**overrides),
        capture_output=True,
        text=True,
        timeout=120,
    )


class LocalLoginDefaultTests(unittest.TestCase):
    def test_without_a_provider_the_static_token_signs_in(self) -> None:
        results = run_probe()
        self.assertTrue(results["local_login_enabled"])
        self.assertFalse(results["oidc_configured"])
        self.assertEqual(
            results["methods"]["body"], {"local_login": True, "oidc": False}
        )
        self.assertEqual(results["static_token"]["status"], 200)
        self.assertEqual(results["static_token"]["body"]["kind"], "break-glass")

    def test_with_a_provider_the_static_token_is_off_by_default(self) -> None:
        results = run_probe(**OIDC_ENVIRONMENT)
        self.assertFalse(results["local_login_enabled"])
        self.assertTrue(results["oidc_configured"])
        self.assertEqual(
            results["methods"]["body"], {"local_login": False, "oidc": True}
        )
        # 🔴 Acceptance criterion 2: the credential is refused at the API, not
        # merely hidden in the Console. SANDBOX_CONTROL_PLANE_TOKEN is still set in the
        # environment of this very process.
        self.assertEqual(results["static_token"]["status"], 401)
        self.assertFalse(results["control_plane_token_is_loaded"])

    def test_the_break_glass_path_can_be_switched_back_on(self) -> None:
        results = run_probe(**OIDC_ENVIRONMENT, SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED="true")
        self.assertTrue(results["local_login_enabled"])
        self.assertEqual(results["static_token"]["status"], 200)

    def test_switching_the_static_token_off_leaves_api_keys_working(self) -> None:
        """The switch closes the emergency door and nothing else.

        🔴 This is the assumption an operator is most likely to get wrong, and
        getting it wrong in the other direction - believing the door is shut
        while it is open - is the failure shape this project keeps hitting. Both
        halves are asserted in one run, against one Control Plane: the static token is
        refused **and** an ordinary API key still authenticates. A test that
        only checked the first would stay green if the switch quietly cut off
        every integrator, and a deployment would discover that in production.
        """
        results = run_probe(store=True, **OIDC_ENVIRONMENT)
        self.assertFalse(results["local_login_enabled"])
        self.assertTrue(results["store_configured"])
        self.assertEqual(results["static_token"]["status"], 401)
        api_key = results["api_key"]
        self.assertEqual(api_key["status"], 200, api_key)
        self.assertEqual(api_key["body"]["kind"], "tenant")
        self.assertEqual(api_key["body"]["tenant_id"], "acme")

    def test_the_documentation_says_what_the_switch_does_not_cover(self) -> None:
        # An operator reads the settings table, not the source. The behavior
        # above is only safe if it is also the documented behavior.
        configuration = (ROOT / "docs/CONFIGURATION.md").read_text(encoding="utf-8")
        self.assertIn("SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED=false", configuration)
        self.assertIn("does not close every non-OIDC way in", configuration)
        self.assertIn("API keys issued by this", configuration)

    def test_no_credential_is_refused(self) -> None:
        # Acceptance criterion 1, over the network rather than by unreachability.
        for results in (run_probe(), run_probe(**OIDC_ENVIRONMENT)):
            self.assertEqual(results["anonymous"]["status"], 401)
            self.assertEqual(results["anonymous"]["body"], {"error": "unauthorized"})

    def test_the_login_redirect_exists_only_with_a_provider(self) -> None:
        self.assertEqual(run_probe()["oidc_login"]["status"], 404)


class FederationRemovalTests(unittest.TestCase):
    """The federated-assertion path is gone, not merely unused.

    It made another service the identity provider for this one, and used that
    service's bearer token as the signing key. Leaving the module importable
    would let one route be re-wired to it without anything noticing.
    """

    SKIP = {".git", ".venv", "node_modules", "__pycache__", "dist"}
    SUFFIXES = {".py", ".ts", ".tsx", ".yaml", ".yml", ".json", ".conf", ".sh"}

    def test_no_source_still_mentions_the_federation_path(self) -> None:
        offenders = []
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT)
            if (
                self.SKIP.intersection(relative.parts)
                or not path.is_file()
                or (path.suffix not in self.SUFFIXES
                    and path.name not in {"Dockerfile", "Makefile"})
            ):
                continue
            if path == pathlib.Path(__file__).resolve():
                # This file names the markers in order to look for them.
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for marker in ("control_sso", "control-sso", "CONTROL_SSO",
                           "federated_admin", "SANDBOX_CONTROL_PLANE_LEGACY_TOKEN_ENABLED"):
                if marker in text:
                    offenders.append(f"{relative}: {marker}")
        self.assertEqual(offenders, [])

    def test_the_module_is_gone(self) -> None:
        self.assertFalse((ROOT / "control_plane/control_sso.py").exists())


class StartupRefusalTests(unittest.TestCase):
    def test_disabling_every_sign_in_method_refuses_to_start(self) -> None:
        result = run_startup(SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED="false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED", result.stderr)
        self.assertIn("SANDBOX_CONTROL_PLANE_OIDC_ISSUER", result.stderr)

    def test_a_provider_without_an_audience_refuses_to_start(self) -> None:
        environment = dict(OIDC_ENVIRONMENT)
        environment.pop("SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE")
        result = run_startup(**environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE", result.stderr)

    def test_a_complete_provider_configuration_starts(self) -> None:
        result = run_startup(**OIDC_ENVIRONMENT)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tenant_only_oidc_cannot_replace_the_last_admin_login(self) -> None:
        environment = dict(OIDC_ENVIRONMENT)
        environment.pop("SANDBOX_CONTROL_PLANE_OIDC_ADMIN_GROUPS")
        environment["SANDBOX_CONTROL_PLANE_OIDC_TENANT_CLAIM"] = "tenant"
        result = run_startup(**environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SANDBOX_CONTROL_PLANE_OIDC_ADMIN_GROUPS", result.stderr)
        self.assertIn("tenant-only", result.stderr)

    def test_tenant_only_oidc_is_allowed_with_break_glass_admin(self) -> None:
        environment = dict(OIDC_ENVIRONMENT)
        environment.pop("SANDBOX_CONTROL_PLANE_OIDC_ADMIN_GROUPS")
        environment["SANDBOX_CONTROL_PLANE_OIDC_TENANT_CLAIM"] = "tenant"
        environment["SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED"] = "true"
        result = run_startup(**environment)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
