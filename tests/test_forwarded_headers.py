"""Nothing this platform issues may cross into a sandbox.

A sandbox runs a tenant's own code. A Console session cookie or a control-plane
bearer token that reaches it is a credential handed over, with no exploit
involved - which is why Gitpod, code-server and Daytona each landed on the same
rule independently.

The second property is subtler: the caller's **own** cookies must survive
byte for byte. Re-serializing a Cookie header through http.cookies re-encodes
values, so an application would read back something different from what it set.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_control_plane_module():
    """Import control_plane/core.py far enough to reach its pure header helpers.

    🔴 The environment is restored afterwards. Import-time settings and a
    patched Kubernetes client leaking out of this module were how an unrelated
    contract test started failing only when the whole suite ran together.
    The volume role is used because it constructs no Kubernetes client at all.
    """
    import os

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT))
    snapshot = dict(os.environ)
    os.environ.update({
        "SANDBOX_CONTROL_PLANE_ROLE": "volume",
        "SANDBOX_CONTROL_PLANE_TOKEN": "test-token",
        "SIGNING_KEY": "0" * 32,
        "WORKSPACE_ID_KEY": "1" * 32,
        "VOLUME_AGENT_URL": "http://127.0.0.1:1",
        "VOLUME_AGENT_TOKEN": "test-volume-token",
        "OBJECT_STORE_ENDPOINT": "http://127.0.0.1:1",
        "OBJECT_STORE_ACCESS_KEY": "test-access",
        "OBJECT_STORE_SECRET_KEY": "test-secret",
    })
    try:
        path = ROOT / "control_plane/core.py"
        spec = importlib.util.spec_from_file_location(
            "control_plane._test_headers_core", path
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


control_plane = load_control_plane_module()


PLATFORM_COOKIES = (
    "sandbox_console_session=abc",
    "sandbox_console_csrf=def",
    "__Host-sandbox_console_session=ghi",
    "__Secure-sandbox_console_csrf=jkl",
    "sandbox_control_session=mno",
    "__Host-sandbox_control_session=pqr",
)


class CookieStrippingTests(unittest.TestCase):
    def test_every_platform_cookie_form_is_removed(self) -> None:
        header = "; ".join((*PLATFORM_COOKIES, "app_theme=dark"))
        self.assertEqual(
            control_plane.strip_platform_cookies(header), "app_theme=dark"
        )

    def test_the_host_prefixed_variant_is_covered(self) -> None:
        # __Host- and __Secure- prefixed names are different cookies to the
        # browser; matching only the bare name leaves the session behind.
        for cookie in PLATFORM_COOKIES:
            with self.subTest(cookie=cookie):
                self.assertEqual(control_plane.strip_platform_cookies(cookie), "")

    def test_the_callers_own_cookies_pass_through_unchanged(self) -> None:
        # Values that http.cookies would quote or percent-encode on output.
        original = (
            'sid="q u o t e d"; tracking=a,b; empty=; '
            'json={"k":"v"}; escaped=%2Fpath%2F'
        )
        self.assertEqual(control_plane.strip_platform_cookies(original), original)

    def test_a_cookie_merely_starting_with_a_similar_name_survives(self) -> None:
        self.assertEqual(
            control_plane.strip_platform_cookies("sandbox_consoles=1"),
            "sandbox_consoles=1",
        )


class ForwardableHeaderTests(unittest.TestCase):
    def forward(self, headers: dict, allowed=("Accept", "Mcp-Method")) -> dict:
        return control_plane.forwardable_headers(headers, allowed)

    def test_platform_identity_headers_never_cross(self) -> None:
        forwarded = self.forward({
            "Accept": "application/json",
            "Authorization": "Bearer control-plane-key",
            "X-Sandbox-Tenant": "tenant-a",
            "X-Acting-Subject": "a" * 32,
            "X-Console-CSRF": "csrf-token",
            "Cookie": "sandbox_console_session=abc; app_theme=dark",
        })
        self.assertEqual(
            forwarded, {"Accept": "application/json", "Cookie": "app_theme=dark"}
        )

    def test_only_the_allow_list_crosses(self) -> None:
        forwarded = self.forward({
            "Accept": "application/json",
            "X-Request-Id": "r-1",
            "Mcp-Method": "tools/call",
        })
        self.assertEqual(
            forwarded, {"Accept": "application/json", "Mcp-Method": "tools/call"}
        )

    def test_a_platform_header_cannot_be_put_on_the_allow_list(self) -> None:
        # The mistake this catches is adding "Authorization" to the allow list
        # of a future proxy route, which would forward the caller's key.
        for name in ("Authorization", "Cookie", "X-Acting-Subject"):
            with self.subTest(header=name):
                with self.assertRaises(ValueError):
                    self.forward({name: "value"}, (name,))

    def test_no_cookie_header_is_produced_when_nothing_survives(self) -> None:
        forwarded = self.forward({"Cookie": "sandbox_console_session=abc"})
        self.assertEqual(forwarded, {})


class ProxyCallSiteTests(unittest.TestCase):
    def test_the_runtime_proxy_builds_its_headers_through_the_helper(self) -> None:
        source = (ROOT / "control_plane/api.py").read_text(encoding="utf-8")
        proxy = source.split("def proxy_runtime_mcp", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("control_plane.forwardable_headers", proxy)
        # Copying headers straight off the request is the shape this replaced.
        self.assertNotIn('self.headers.get("Cookie"', proxy)
        self.assertNotIn('self.headers["Mcp-Name"]', proxy)


if __name__ == "__main__":
    unittest.main()
