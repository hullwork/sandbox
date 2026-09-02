from __future__ import annotations

import os
import unittest
from unittest import mock

from sandbox_platform import sandbox_client


class ObjectOwnerTests(unittest.TestCase):
    """The client does not decide which partition an object goes in.

    It used to compose one from a principal it was handed, which put the tenant
    segment of a persisted key in the caller's gift. The platform derives both
    segments now and refuses a tenant-bound credential that names one, so a
    client that still sent a plausible value would have every object call
    refused - and the value it sent was a real identity crossing an
    organizational boundary, which is the reason the derivation exists.
    """

    def test_the_client_composes_no_owner_of_its_own(self) -> None:
        self.assertFalse(hasattr(sandbox_client, "object_owner"))

    def test_an_upload_names_no_owner_unless_one_is_passed(self) -> None:
        manager = sandbox_client.SandboxManager()
        seen: list[dict] = []

        def capture(method, path, *, payload=None, **kwargs):
            seen.append({"path": path, "payload": payload})
            raise sandbox_client.ControlPlaneError(503, "stop after the request is built")

        with mock.patch.object(manager, "_request", capture):
            with self.assertRaises(sandbox_client.ControlPlaneError):
                manager.put_agent_blob("agent", "run", "outputs/x", b"hi")
            with self.assertRaises(sandbox_client.ControlPlaneError):
                manager.put_agent_blob(
                    "agent", "run", "outputs/x", b"hi", owner="legacy-host/alice"
                )

        self.assertEqual(seen[0]["path"], "/v1/storage/tickets")
        self.assertNotIn("owner", seen[0]["payload"])
        self.assertEqual(seen[1]["payload"]["owner"], "legacy-host/alice")


class WorkspacePathNormalisationTests(unittest.TestCase):
    """The one path-normalisation point on the client side.

    Every file_* tool in the MCP bridge goes through it. Nothing here is
    security critical - the server re-checks - but a broken normaliser turns
    a client-side ValueError into a server-side 400 with no test to say so.
    """

    def test_workspace_paths_are_made_relative(self) -> None:
        for given, expected in (
            ("/workspace", ""),
            ("/workspace/x", "x"),
            ("/workspace/a/b.txt", "a/b.txt"),
            ("x", "x"),
            ("a/b.txt", "a/b.txt"),
        ):
            with self.subTest(path=given):
                self.assertEqual(
                    sandbox_client.normalize_workspace_path(given), expected
                )

    def test_paths_outside_the_workspace_are_refused_by_this_function(self) -> None:
        # The message pins which check answered: an absolute path that is not
        # under /workspace, a home path, and an empty string each have their
        # own branch and their own wording.
        for given, message in (
            ("/etc/passwd", "only /workspace paths"),
            ("/workspaces/x", "only /workspace paths"),
            ("~/x", "home paths"),
            ("~", "home paths"),
            ("", "non-empty string"),
        ):
            with self.subTest(path=given):
                with self.assertRaisesRegex(ValueError, message):
                    sandbox_client.normalize_workspace_path(given)


class ConfigurationTests(unittest.TestCase):
    def test_missing_control_plane_token_fails_closed(self) -> None:
        manager = sandbox_client.SandboxManager()
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            _ = manager.control_plane_token


class RuntimeLookupTests(unittest.TestCase):
    def test_resolve_workspace_uses_exact_non_creating_endpoint(self) -> None:
        manager = sandbox_client.SandboxManager()
        lease = sandbox_client.Lease(session_id="session-id")
        with (
            mock.patch.object(manager, "_lease", return_value=lease),
            mock.patch.object(
                manager,
                "_request",
                return_value=(
                    {
                        "workspace_id": "ws-000000000001",
                        "sandbox_id": None,
                        "template": None,
                    },
                    "application/json",
                ),
            ) as request,
        ):
            result, resolved = manager.resolve_workspace("demo")
        self.assertEqual(result.workspace_id, "ws-000000000001")
        self.assertIsNone(resolved["sandbox_id"])
        request.assert_called_once_with(
            "POST",
            "/v1/workspaces/resolve",
            payload={"session_id": "session-id"},
        )

    def test_lookup_runtime_rediscovers_runtime_by_workspace(self) -> None:
        manager = sandbox_client.SandboxManager()
        lease = sandbox_client.Lease(
            session_id="session-id",
            workspace_id="ws-000000000001",
        )
        with (
            mock.patch.object(
                manager,
                "resolve_workspace",
                return_value=(
                    lease,
                    {
                        "workspace_id": "ws-000000000001",
                        "sandbox_id": "sb-000000000002",
                        "template": "python",
                    },
                ),
            ),
            mock.patch.object(
                manager,
                "_request",
                return_value=(
                    {"access_token": "scoped", "access_token_expires_in": 600},
                    "application/json",
                ),
            ) as request,
        ):
            result = manager.lookup_runtime("demo")
        self.assertEqual(result.sandbox_id, "sb-000000000002")
        self.assertEqual(result.sandbox_template, "python")
        request.assert_called_once_with(
            "POST", "/v1/sandboxes/sb-000000000002/token", payload={}
        )

    def test_lookup_runtime_does_not_create_a_runtime(self) -> None:
        manager = sandbox_client.SandboxManager()
        lease = sandbox_client.Lease(
            session_id="session-id",
            workspace_id="ws-000000000001",
        )
        with (
            mock.patch.object(
                manager,
                "resolve_workspace",
                return_value=(
                    lease,
                    {
                        "workspace_id": "ws-000000000001",
                        "sandbox_id": None,
                        "template": None,
                    },
                ),
            ),
            mock.patch.object(manager, "ensure_runtime") as ensure_runtime,
            self.assertRaises(sandbox_client.ControlPlaneError) as raised,
        ):
            manager.lookup_runtime("demo")
        self.assertEqual(raised.exception.status, 404)
        ensure_runtime.assert_not_called()


class BrokenResponseTests(unittest.TestCase):
    """A response missing a promised field is a 502, never a KeyError.

    The CLI's except tuple has no KeyError in it and the MCP bridge would
    report one as an internal error; list_runtimes already treats a missing
    list as 502, and the lease-creating calls now agree with it.
    """

    def manager_with_response(self, payload: dict) -> sandbox_client.SandboxManager:
        manager = sandbox_client.SandboxManager()
        lease = sandbox_client.Lease(session_id="session-id")
        self.enterContext(mock.patch.object(manager, "_lease", return_value=lease))
        self.enterContext(
            mock.patch.object(
                manager, "_request", return_value=(payload, "application/json")
            )
        )
        return manager

    def test_a_workspace_response_without_a_token_is_a_502(self) -> None:
        manager = self.manager_with_response({"workspace_id": "ws-000000000001"})
        with self.assertRaises(sandbox_client.ControlPlaneError) as caught:
            manager.ensure_workspace("demo")
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("access_token", str(caught.exception))

    def test_a_workspace_response_without_an_id_is_a_502(self) -> None:
        manager = self.manager_with_response({"access_token": "scoped"})
        with self.assertRaises(sandbox_client.ControlPlaneError) as caught:
            manager.ensure_workspace("demo")
        self.assertEqual(caught.exception.status, 502)

    def test_a_runtime_response_without_an_id_is_a_502(self) -> None:
        manager = sandbox_client.SandboxManager()
        lease = sandbox_client.Lease(
            session_id="session-id", workspace_id="ws-000000000001"
        )
        with (
            mock.patch.object(manager, "ensure_workspace", return_value=lease),
            mock.patch.object(
                manager,
                "_request",
                return_value=({"access_token": "scoped"}, "application/json"),
            ),
        ):
            with self.assertRaises(sandbox_client.ControlPlaneError) as caught:
                manager.ensure_runtime("demo")
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("no id", str(caught.exception))
        self.assertIsNone(lease.sandbox_id)

    def test_a_token_response_without_a_token_is_a_502_on_lookup(self) -> None:
        manager = sandbox_client.SandboxManager()
        lease = sandbox_client.Lease(
            session_id="session-id", workspace_id="ws-000000000001"
        )
        with (
            mock.patch.object(
                manager,
                "resolve_workspace",
                return_value=(
                    lease,
                    {"workspace_id": "ws-000000000001", "sandbox_id": "sb-000000000002"},
                ),
            ),
            mock.patch.object(
                manager, "_request", return_value=({}, "application/json")
            ),
        ):
            with self.assertRaises(sandbox_client.ControlPlaneError) as caught:
                manager.lookup_runtime("demo")
        self.assertEqual(caught.exception.status, 502)


class SandboxFacadeTests(unittest.TestCase):
    def test_run_command_quotes_each_argument_and_maps_result(self) -> None:
        manager = mock.Mock()
        manager.shell.return_value = {
            "exit_code": 7,
            "stdout": "out",
            "stderr": "err",
            "timed_out": False,
            "output_truncated": True,
        }
        sandbox = sandbox_client.Sandbox("demo", manager=manager)
        result = sandbox.run_command("printf", ["%s", "a; echo unsafe"])
        manager.shell.assert_called_once_with(
            "printf %s 'a; echo unsafe'", timeout_seconds=30
        )
        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.stdout, "out")
        self.assertEqual(result.stderr, "err")
        self.assertTrue(result.output_truncated)

    def test_get_without_resume_only_looks_up_active_runtime(self) -> None:
        manager = mock.Mock()
        sandbox = sandbox_client.Sandbox.get("demo", manager=manager)
        manager.lookup_runtime.assert_called_once_with("demo")
        manager.ensure_runtime.assert_not_called()
        self.assertEqual(sandbox.name, "demo")

    def test_create_binds_name_to_runtime(self) -> None:
        manager = mock.Mock()
        manager.lookup_runtime.side_effect = sandbox_client.ControlPlaneError(404, "missing")
        sandbox_client.Sandbox.create(
            "demo", manager=manager, template="python"
        )
        manager.ensure_runtime.assert_called_once_with("demo", template="python")

    def test_create_reuses_an_active_named_runtime(self) -> None:
        manager = mock.Mock()
        manager.lookup_runtime.return_value = sandbox_client.Lease(
            session_id="session-id",
            sandbox_id="sb-000000000002",
            sandbox_template="python",
        )
        sandbox_client.Sandbox.create(
            "demo", manager=manager, template="python"
        )
        manager.ensure_runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
