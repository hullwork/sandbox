"""The owner partition every object key carries (control_plane/core.py).

Every key is ``users/<tenant>/<subject>/...``. That partition is not a header
that lasts one call -- it is the prefix the bytes live under for as long as
they exist, so a caller who can choose it can choose where its neighbours' data
goes. ``test_object_owner_derivation.py`` covers the route layer deciding *who*
the owner is. Nothing covered the layer underneath: ``validate_object_owner``,
``validate_object_path``, ``object_location``, ``object_key_owner``,
``bind_object_owner``, ``issue_object_ticket``, ``verify_object_ticket`` and
``object_slot`` all had zero callers in this suite.

That layer is the last thing between a caller-supplied string and the key
handed to ``mc``, which is why it is tested directly here rather than only
through a route: a 403 from the route cannot tell "refused" apart from "carried
out under a different owner than the caller believes", and the second outcome
is what the partition exists to prevent.

``consume_object_ticket`` is not covered here -- it writes a Kubernetes Lease
and needs a client double rather than a pure call. It remains uncovered.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import pathlib
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_api_role():
    """Import the real core in the api role, then leave the namespace as found.

    Same discipline as ``test_volume_local_files``: ``core.py`` reads its
    configuration at import, the Kubernetes client is built at import, and
    ``reaper.py`` resolves ``from . import core`` through the parent package's
    attribute before ``sys.modules``. Leaving that attribute bound makes
    ``test_reaper_behavior``'s fake core invisible, so the import is unwound
    and the module is kept alive only through the name returned here.
    """
    package = importlib.import_module("control_plane")
    kube = importlib.import_module("control_plane.kube")
    preloaded = {name for name in sys.modules if name.startswith("control_plane")}
    required = {
        "SANDBOX_CONTROL_PLANE_ROLE": "api",
        "SANDBOX_CONTROL_PLANE_TOKEN": "test-control-plane-token",
        "SIGNING_KEY": "test-signing-key",
        "OBJECT_STORE_ENDPOINT": "http://127.0.0.1:9000",
        "OBJECT_STORE_ACCESS_KEY": "test-access",
        "OBJECT_STORE_SECRET_KEY": "test-secret",
        "WORKSPACE_ID_KEY": "test-workspace-id-key",
    }
    previous_env = {name: os.environ.get(name) for name in required}
    previous_client = kube.KubeClient
    os.environ.update(required)
    # No cluster here, and none of these functions touch one.
    kube.KubeClient = lambda *args, **kwargs: None
    try:
        from control_plane import core
    finally:
        kube.KubeClient = previous_client
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        for name in [n for n in sys.modules if n.startswith("control_plane")]:
            if name in preloaded:
                continue
            del sys.modules[name]
            attribute = name.rpartition(".")[2]
            if getattr(package, attribute, None) is not None:
                delattr(package, attribute)
    assert core.CONTROL_PLANE_ROLE == "api", (
        f"control_plane.core was already imported as role "
        f"{core.CONTROL_PLANE_ROLE!r}; this module would be testing that "
        "configuration instead"
    )
    assert "control_plane.core" not in sys.modules, "core was left loaded"
    assert not hasattr(package, "core"), "the package still points at this core"
    return core


core = load_api_role()

OWNER = "acme/alice"
OTHER = "rival/mallory"
LOCATOR = {"scope": "agent", "agent_id": "agent-1", "run_id": "run-1"}


class OwnerValidationTests(unittest.TestCase):
    def test_the_accepted_forms_cover_what_the_auth_layer_produces(self) -> None:
        for owner in (
            "acme/alice",
            "acme/alice@example.com",
            "t-1/user_name",
            "A/B",
            f"{'a' * 128}/{'b' * 128}",
        ):
            with self.subTest(owner=owner):
                self.assertEqual(core.validate_object_owner(owner), owner)

    def test_traversal_and_malformed_owners_are_refused(self) -> None:
        for owner in (
            "",
            None,
            "acme",
            "acme/alice/extra",
            "../etc",
            "acme/../../x",
            "/acme/alice",
            "acme//alice",
            "-acme/alice",
            "acme/alice\x00",
            "acme/alice bob",
            f"{'a' * 129}/bob",
        ):
            with self.subTest(owner=owner):
                with self.assertRaises(ValueError):
                    core.validate_object_owner(owner)

    def test_dot_only_segments_are_refused_twice_over(self) -> None:
        """The character class and the explicit check both have to hold.

        A leading alphanumeric already makes a dot-only segment impossible, so
        this looks redundant -- until someone widens the class. The explicit
        check is what keeps ``users/../../x`` from escaping the prefix the
        moment any layer normalises the path.
        """
        for owner in ("./alice", "acme/.", "../..", "./."):
            with self.subTest(owner=owner):
                with self.assertRaises(ValueError):
                    core.validate_object_owner(owner)

    def test_the_dot_guard_holds_even_with_the_pattern_widened(self) -> None:
        # Proves the second check is load-bearing rather than shadowed: with
        # the pattern accepting anything, the dot segments must still be
        # refused by the explicit guard.
        import re

        with mock.patch.object(core, "OBJECT_OWNER", re.compile(r"^.*$")):
            self.assertEqual(core.validate_object_owner(OWNER), OWNER)
            for owner in ("./alice", "acme/.", "../.."):
                with self.subTest(owner=owner):
                    with self.assertRaises(ValueError):
                        core.validate_object_owner(owner)


class ObjectPathTests(unittest.TestCase):
    def test_a_path_escaping_its_prefix_is_refused(self) -> None:
        for path in ("../outside", "/etc/passwd", "outputs/../../x"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "escapes its prefix"):
                    core.validate_object_path(
                        path, allowed_roots={"outputs"}
                    )

    def test_a_nul_byte_and_an_empty_path_are_refused(self) -> None:
        for path in ("", None, "outputs/a\x00b"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "object path is required"):
                    core.validate_object_path(path, allowed_roots={"outputs"})

    def test_the_first_segment_must_be_an_allowed_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "must start with one of"):
            core.validate_object_path("secrets/key", allowed_roots={"outputs"})
        self.assertEqual(
            core.validate_object_path("outputs/report.txt", allowed_roots={"outputs"}),
            "outputs/report.txt",
        )

    def test_the_prefix_itself_is_addressable_only_when_allowed(self) -> None:
        self.assertEqual(
            core.validate_object_path(".", allowed_roots={"outputs"}, allow_root=True),
            ".",
        )
        with self.assertRaises(ValueError):
            core.validate_object_path(".", allowed_roots={"outputs"})


class ObjectLocationTests(unittest.TestCase):
    def test_an_upload_key_is_partitioned_by_owner(self) -> None:
        bucket, key = core.object_location({
            "owner": OWNER, "scope": "upload",
            "upload_id": "up-1", "path": "source/data.csv",
        })
        self.assertEqual(bucket, core.OBJECT_STORE_UPLOAD_BUCKET)
        self.assertEqual(key, f"users/{OWNER}/uploads/up-1/source/data.csv")

    def test_an_agent_key_is_partitioned_by_owner(self) -> None:
        bucket, key = core.object_location({
            **LOCATOR, "owner": OWNER, "path": "outputs/report.txt",
        })
        self.assertEqual(bucket, core.OBJECT_STORE_AGENT_BUCKET)
        self.assertEqual(
            key, f"users/{OWNER}/agents/agent-1/runs/run-1/outputs/report.txt"
        )

    def test_a_prefix_listing_keeps_the_partition(self) -> None:
        _, key = core.object_location(
            {**LOCATOR, "owner": OWNER}, allow_prefix=True
        )
        self.assertEqual(key, f"users/{OWNER}/agents/agent-1/runs/run-1")

    def test_the_owner_is_checked_before_the_scope(self) -> None:
        # Order matters: reporting "scope must be upload or agent" for a
        # malformed owner tells the caller to fix the wrong field, and leaves
        # the owner unchecked on any path that later grows a third scope.
        with self.assertRaisesRegex(ValueError, "owner must be"):
            core.object_location({"owner": "../etc", "scope": "nonsense"})

    def test_a_missing_owner_is_refused_for_every_scope(self) -> None:
        for payload in (
            {"scope": "upload", "upload_id": "up-1"},
            dict(LOCATOR),
            {"scope": "nonsense"},
        ):
            with self.subTest(scope=payload.get("scope")):
                with self.assertRaisesRegex(ValueError, "owner must be"):
                    core.object_location(payload)

    def test_an_owner_cannot_smuggle_a_path_out_of_its_prefix(self) -> None:
        for owner in ("acme/alice/../../bob", "acme/../bob", "../acme/alice"):
            with self.subTest(owner=owner):
                with self.assertRaises(ValueError):
                    core.object_location({**LOCATOR, "owner": owner})


class ObjectKeyOwnerTests(unittest.TestCase):
    def test_the_owner_is_readable_back_out_of_a_key(self) -> None:
        for scope_root in ("uploads", "agents"):
            with self.subTest(root=scope_root):
                key = f"users/{OWNER}/{scope_root}/x/y"
                self.assertEqual(core.object_key_owner(key), OWNER)

    def test_a_foreign_or_legacy_key_has_no_owner(self) -> None:
        for key in (
            None,
            42,
            "",
            "users/acme/alice",
            "workspaces/ws-1/file.txt",
            "users/acme/alice/secrets/x",   # not an owner root
            "other/acme/alice/agents/x/y",
            "users/../../alice/agents/x/y",
        ):
            with self.subTest(key=key):
                self.assertIsNone(core.object_key_owner(key))


class BindObjectOwnerTests(unittest.TestCase):
    """A workspace token may only touch the owner it was issued for."""

    def test_a_token_without_an_owner_claim_is_refused(self) -> None:
        # This path once fell back to reading the owner from the body, which
        # gave a delegated token the reach of an admin one.
        with self.assertRaisesRegex(ValueError, "carries no owner claim"):
            core.bind_object_owner({"owner": OWNER}, None)

    def test_a_body_naming_another_owner_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            core.bind_object_owner({"owner": OTHER}, OWNER)

    def test_the_owner_is_filled_in_when_the_body_omits_it(self) -> None:
        self.assertEqual(
            core.bind_object_owner({"path": "outputs/a"}, OWNER),
            {"path": "outputs/a", "owner": OWNER},
        )

    def test_a_matching_body_is_left_alone(self) -> None:
        self.assertEqual(
            core.bind_object_owner({"owner": OWNER}, OWNER)["owner"], OWNER
        )


class ObjectTicketTests(unittest.TestCase):
    def ticket(self, **overrides) -> dict:
        payload = {**LOCATOR, "owner": OWNER, "path": "outputs/report.txt",
                   "operation": "upload", **overrides}
        return core.issue_object_ticket(payload)

    def test_a_ticket_carries_the_owner_partitioned_key(self) -> None:
        issued = self.ticket()
        self.assertEqual(
            issued["object"]["key"],
            f"users/{OWNER}/agents/agent-1/runs/run-1/outputs/report.txt",
        )
        claims = core.verify_object_ticket(issued["access_token"], "upload")
        self.assertIsNotNone(claims)
        self.assertEqual(core.object_key_owner(claims["key"]), OWNER)

    def test_the_ttl_is_capped_and_a_shorter_request_is_honoured(self) -> None:
        self.assertEqual(
            self.ticket(expires_in=30)["expires_in"], 30
        )
        with self.assertRaisesRegex(ValueError, "expires_in must be"):
            self.ticket(expires_in=core.OBJECT_TICKET_TTL_SECONDS + 1)
        with self.assertRaisesRegex(ValueError, "expires_in must be"):
            self.ticket(expires_in=0)

    def test_a_ticket_for_one_operation_does_not_verify_as_the_other(self) -> None:
        issued = self.ticket(operation="upload")
        self.assertIsNotNone(core.verify_object_ticket(issued["access_token"], "upload"))
        self.assertIsNone(core.verify_object_ticket(issued["access_token"], "download"))

    def test_a_tampered_signature_is_refused(self) -> None:
        encoded, _, signature = self.ticket()["access_token"].partition(".")
        forged = f"{encoded}.{signature[:-2]}xy"
        self.assertIsNone(core.verify_object_ticket(forged, "upload"))

    def test_a_signed_but_unpartitioned_key_is_refused(self) -> None:
        """The signature alone is not the criterion.

        Anything holding the signing key could otherwise mint a ticket for a
        key outside any owner prefix, which is the whole partition bypassed in
        one step. The claims are checked as well as the signature.
        """
        def mint(key: str) -> str:
            claims = {
                "aud": "sandbox-control-plane",
                "kind": "object-ticket",
                "op": "upload",
                "bucket": core.OBJECT_STORE_AGENT_BUCKET,
                "key": key,
                "max_bytes": 16,
                "content_type": "application/octet-stream",
                "sha256": "",
                "jti": "a" * 32,
                "exp": int(time.time()) + 60,
            }
            encoded = core.b64url_encode(
                json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
            )
            signature = core.b64url_encode(
                hmac.new(
                    core.SIGNING_KEY, encoded.encode("ascii"), hashlib.sha256
                ).digest()
            )
            return f"{encoded}.{signature}"

        # The control: the same minting, under an owner prefix, is accepted --
        # so a rejection below is the claim check and not a bad signature.
        partitioned = mint(f"users/{OWNER}/agents/agent-1/runs/run-1/outputs/a")
        self.assertIsNotNone(core.verify_object_ticket(partitioned, "upload"))

        # Legal-looking, but under no owner at all.
        self.assertIsNone(core.verify_object_ticket(mint("secrets/root.key"), "upload"))

    def test_an_expired_ticket_is_refused(self) -> None:
        issued = self.ticket(expires_in=1)
        # core.time is the stdlib module itself, so the replacement has to
        # close over the real function rather than call the patched name.
        real_time = time.time
        with mock.patch.object(core.time, "time", lambda: real_time() + 120):
            self.assertIsNone(
                core.verify_object_ticket(issued["access_token"], "upload")
            )
        # And it verifies again once the clock is back, so the rejection above
        # was the expiry and not the patching.
        self.assertIsNotNone(
            core.verify_object_ticket(issued["access_token"], "upload")
        )

    def test_a_malformed_token_is_refused_without_raising(self) -> None:
        for token in ("", "no-dot", "a.b", "...."):
            with self.subTest(token=token):
                self.assertIsNone(core.verify_object_ticket(token, "upload"))


class ObjectQueueDepthTests(unittest.TestCase):
    """Two gates, and the queue one is what keeps threads from piling up.

    ``_OBJECT_SLOTS`` bounds the object work in flight and the memory it holds;
    ``_OBJECT_QUEUE_SLOTS`` bounds the *request threads* waiting in front of it.
    Without the second, concurrent object operations grow the thread count
    without limit until the 1GiB cgroup kills the Control Plane -- and the
    reaper is a daemon thread in that process, so sandboxes stop being
    reclaimed too.

    These tests must widen the execution gate. It waits when full while the
    queue gate raises, so without widening the second entrant blocks on the
    execution gate and the module hangs instead of failing.
    """

    def gates(self, depth: int):
        return (
            mock.patch.object(
                core, "_OBJECT_QUEUE_SLOTS", threading.BoundedSemaphore(depth)
            ),
            mock.patch.object(
                core, "_OBJECT_SLOTS", threading.BoundedSemaphore(depth + 8)
            ),
        )

    def test_a_full_queue_raises_instead_of_waiting(self) -> None:
        depth = 2
        queue_gate, exec_gate = self.gates(depth)
        with queue_gate, exec_gate:
            held = []
            try:
                for _ in range(depth):
                    slot = core.object_slot()
                    slot.__enter__()
                    held.append(slot)
                began = time.monotonic()
                with self.assertRaises(core.ObjectStoreBusy):
                    with core.object_slot():
                        self.fail("admitted while the queue was full")
                # Fail *fast* is the point: the gate exists so a request thread
                # does not sit here holding its stack while the next hundred
                # arrive. A gate that waits and then raises passes the
                # assertion above while leaving the pile-up in place.
                self.assertLess(
                    time.monotonic() - began,
                    5.0,
                    "the queue gate waited instead of refusing immediately",
                )
            finally:
                for slot in held:
                    slot.__exit__(None, None, None)

    def test_a_slot_is_released_for_reuse(self) -> None:
        # Without release, one burst wedges the Control Plane permanently.
        queue_gate, exec_gate = self.gates(1)
        with queue_gate, exec_gate:
            with core.object_slot():
                with self.assertRaises(core.ObjectStoreBusy):
                    with core.object_slot():
                        pass
            with core.object_slot():
                pass

    def test_busy_is_a_runtime_error_subclass(self) -> None:
        # The handler's trailing `except (OSError, RuntimeError, ValueError)`
        # would otherwise swallow this before the dedicated 503 branch.
        self.assertTrue(issubclass(core.ObjectStoreBusy, RuntimeError))


if __name__ == "__main__":
    unittest.main()
