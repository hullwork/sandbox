"""``POST /v1/workspaces`` reads ``principal: {kind, id}`` and nothing else.

The OpenAPI contract used to declare ``principal_id`` / ``principal_type`` on
``CreateWorkspaceRequest`` while the implementation only ever read the
``principal`` object that ``ResolveWorkspaceRequest`` declared. A caller
written against the contract therefore derived the *same* workspace for every
end user of a session (the default ``service/default`` principal), got the
same scoped token for all of them, and saw no error. The two request schemas
now share one ``Principal`` definition, and the legacy spellings are refused
with 400 rather than ignored, the way ``image`` is refused on template
resolution.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from control_plane_probe import run_probe  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_control_plane_module():
    """Import control_plane/core.py for parse_principal, then put the world back.

    Same construction as test_forwarded_headers: the volume role constructs no
    Kubernetes client, and the environment is restored so import-time settings
    do not leak into other modules of the suite.
    """
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
        spec = importlib.util.spec_from_file_location(
            "control_plane._test_principal_core", ROOT / "control_plane/core.py"
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


class ParsePrincipalTests(unittest.TestCase):
    def test_legacy_top_level_fields_are_refused_not_ignored(self) -> None:
        for payload in (
            {"session_id": "s", "principal_id": "u1"},
            {"session_id": "s", "principal_type": "user"},
            {"session_id": "s", "principal_id": "u1", "principal_type": "user"},
            {"session_id": "s", "principal": {"kind": "user", "id": "u1"}, "principal_id": "u1"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError) as caught:
                control_plane.parse_principal(payload)
            self.assertIn("principal", str(caught.exception))

    def test_legacy_fields_are_refused_even_with_an_acting_subject(self) -> None:
        with self.assertRaises(ValueError):
            control_plane.parse_principal({"principal_id": "u1"}, "a" * 32)

    def test_the_object_form_is_what_is_read(self) -> None:
        self.assertEqual(
            control_plane.parse_principal({"principal": {"kind": "user", "id": "u1"}}),
            ("user", "u1"),
        )
        self.assertEqual(control_plane.parse_principal({}), ("service", "default"))


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = yaml.safe_load(
            (ROOT / "contracts/control-plane-openapi.yaml").read_text(encoding="utf-8")
        )
        cls.schemas = spec["components"]["schemas"]

    def test_create_and_resolve_requests_share_the_principal_schema(self) -> None:
        create = self.schemas["CreateWorkspaceRequest"]["properties"]
        resolve = self.schemas["ResolveWorkspaceRequest"]["properties"]
        self.assertNotIn("principal_id", create)
        self.assertNotIn("principal_type", create)
        self.assertEqual(create["principal"], {"$ref": "#/components/schemas/Principal"})
        self.assertEqual(resolve["principal"], create["principal"])
        self.assertEqual(
            set(self.schemas["Principal"]["properties"]), {"kind", "id"}
        )


class LiveTests(unittest.TestCase):
    """Over HTTP: the legacy spelling is a 400, and the object form separates users."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.results, _ = run_probe(
            """
            call("tenant", "POST", "/v1/admin/tenants", admin, {"id": "acme"})
            key = call("key", "POST", "/v1/admin/tenants/acme/keys", admin, {"label": "k"})["body"]["api_key"]
            call("legacy_id", "POST", "/v1/workspaces", key, {"session_id": "s1", "principal_id": "u1"})
            call("legacy_type", "POST", "/v1/workspaces", key, {"session_id": "s1", "principal_type": "user"})
            call("plain", "POST", "/v1/workspaces", key, {"session_id": "s1"})
            call("user_one", "POST", "/v1/workspaces", key, {"session_id": "s1", "principal": {"kind": "user", "id": "u1"}})
            call("user_two", "POST", "/v1/workspaces", key, {"session_id": "s1", "principal": {"kind": "user", "id": "u2"}})
            call("resolve_legacy", "POST", "/v1/workspaces/resolve", key, {"session_id": "s1", "principal_id": "u1"})
            """
        )

    def test_legacy_fields_are_a_400_before_any_downstream_call(self) -> None:
        for name in ("legacy_id", "legacy_type", "resolve_legacy"):
            with self.subTest(route=name):
                response = self.results[name]
                self.assertEqual(response["status"], 400, response)
                self.assertIn("principal", response["body"]["error"])
                self.assertEqual((response["kube_calls"], response["volume_calls"]), (0, 0))

    def test_principal_objects_separate_users_of_one_session(self) -> None:
        plain = self.results["plain"]
        one = self.results["user_one"]
        two = self.results["user_two"]
        for response in (plain, one, two):
            self.assertEqual(response["status"], 201, response)
        ids = {plain["body"]["workspace_id"], one["body"]["workspace_id"], two["body"]["workspace_id"]}
        self.assertEqual(len(ids), 3, ids)


if __name__ == "__main__":
    unittest.main()
