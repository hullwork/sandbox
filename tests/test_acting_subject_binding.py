"""Which end user a request is for, in a client that serves many at once.

The pseudonym travels in one header, and the header is set in one place. What
this file pins is that the one place is the *ambient scope* rather than the
client object: the client is a process-wide singleton, so an identity held on
it is shared by every request in flight in every thread.

That failure is worth stating plainly, because it does not announce itself. A
request sent under the wrong subject is byte-for-byte a request sent under the
right one - same shape, same 2xx, same log line - and the only visible trace is
that an object ended up in another user's partition, which nobody looks at until
somebody reads a file they should not have. So the test below is not "is the
header present" but "do two subjects in flight at the same time stay apart",
run with a handshake that forces the overlap rather than hoping for it.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from sandbox_platform import sandbox_client


ROOT = pathlib.Path(__file__).resolve().parents[1]
SUBJECT_A = "a" * 32
SUBJECT_B = "b" * 32
#: What a conforming deriver actually emits, from the cross-repository vector.
#: This client refuses a malformed pseudonym, so it has to be held to the same
#: values the platform is held to - a client that rejected a correct derivation
#: would be invisible from the deriving side, which sees a well-formed value go
#: out and an error come back saying nothing about which end refused it.
VECTOR_SUBJECTS = [
    vector["expected"]
    for vector in json.loads(
        (ROOT / "docs/acting-subject-vectors.json").read_text(encoding="utf-8")
    )["vectors"]
]


class EchoHandler(BaseHTTPRequestHandler):
    """Report back which subject the request actually arrived with."""

    def _respond(self) -> None:
        body = json.dumps(
            {
                "status": "ok",
                "seen": self.headers.get("X-Acting-Subject"),
                "authorization": self.headers.get("Authorization"),
            }
        ).encode("utf-8")
        length = self.headers.get("Content-Length")
        if length:
            self.rfile.read(int(length))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond
    do_DELETE = _respond

    def log_message(self, *_args) -> None:
        return


@contextlib.contextmanager
def echo_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with mock.patch.dict(
            os.environ,
            {
                "SANDBOX_CONTROL_PLANE_URL": f"http://127.0.0.1:{server.server_port}",
                "SANDBOX_TOKEN": "test-token",
            },
        ):
            yield
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


OBJECT_CALL = ("GET", "/v1/storage/objects/stat")


class SubjectIsolationTests(unittest.TestCase):
    def test_two_scopes_in_flight_do_not_exchange_subjects(self) -> None:
        """The property a process-wide identity cannot have.

        The handshake is the test. Both threads are inside their own scope
        before either sends, so an identity stored anywhere shared has already
        been overwritten by the second one when the first sends - which is the
        state a running server reaches by itself, under load, without anything
        looking unusual.
        """
        manager = sandbox_client.SandboxManager()
        a_ready = threading.Event()
        b_bound = threading.Event()
        a_done = threading.Event()
        seen: dict[str, object] = {}

        def call() -> object:
            result, _ = manager._request(
                *OBJECT_CALL, query={"scope": "agent"}
            )
            return result.get("seen")

        def first() -> None:
            try:
                with sandbox_client.acting_subject_context(SUBJECT_A):
                    a_ready.set()
                    b_bound.wait(10)
                    seen["a"] = call()
            except Exception as exc:  # surfaced by the assertions below
                seen["a"] = f"raised: {exc!r}"
            finally:
                a_done.set()

        def second() -> None:
            try:
                a_ready.wait(10)
                with sandbox_client.acting_subject_context(SUBJECT_B):
                    b_bound.set()
                    seen["b"] = call()
                    a_done.wait(10)
            except Exception as exc:
                seen["b"] = f"raised: {exc!r}"

        with echo_server():
            threads = [
                threading.Thread(target=first),
                threading.Thread(target=second),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(seen.get("a"), SUBJECT_A, seen)
        self.assertEqual(seen.get("b"), SUBJECT_B, seen)

    def test_an_unbound_scope_names_nobody(self) -> None:
        manager = sandbox_client.SandboxManager()
        with echo_server():
            result, _ = manager._request("GET", "/v1/workspaces")
        self.assertIsNone(result["seen"])

    def test_the_header_leaves_with_the_request(self) -> None:
        manager = sandbox_client.SandboxManager()
        with echo_server(), sandbox_client.acting_subject_context(SUBJECT_A):
            result, _ = manager._request("GET", "/v1/workspaces")
        self.assertEqual(result["seen"], SUBJECT_A)


class MalformedPseudonymTests(unittest.TestCase):
    """Refused at the bind, where the derivation that produced it is.

    Every wrong derivation produces something that looks like a pseudonym.
    Truncating the hex string rather than the digest gives 16 characters; the
    same bytes rendered in uppercase are the same bytes. Both reach the platform
    as a 400 on some later call, by which point the code that derived them is no
    longer on the stack.
    """

    def test_a_conforming_derivation_is_accepted(self) -> None:
        # Without this, an emptied vector file makes the loop run zero times.
        self.assertGreaterEqual(len(VECTOR_SUBJECTS), 2)
        for pseudonym in VECTOR_SUBJECTS:
            with self.subTest(pseudonym=pseudonym):
                with sandbox_client.acting_subject_context(pseudonym):
                    self.assertEqual(
                        sandbox_client.current_acting_subject(), pseudonym
                    )

    def test_the_shapes_a_wrong_derivation_produces_are_refused(self) -> None:
        for value in (
            SUBJECT_A[:16],
            SUBJECT_A.upper(),
            "z" * 32,
            "a" * 16 + ":" + "a" * 15,
            "",
            None,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                with sandbox_client.acting_subject_context(value):
                    pass

    def test_the_scope_is_unwound_even_when_the_body_raises(self) -> None:
        with self.assertRaises(ZeroDivisionError):
            with sandbox_client.acting_subject_context(SUBJECT_A):
                raise ZeroDivisionError
        self.assertIsNone(sandbox_client.current_acting_subject())


class ObjectCallsNameSomebodyTests(unittest.TestCase):
    """An object call with no identity fails here, not at the platform.

    The platform's 400 is correct and arrives with the causal chain already
    gone: not which call site, not for which user, not why nothing was bound.
    """

    def test_an_object_call_without_a_subject_is_refused_before_it_is_sent(
        self,
    ) -> None:
        manager = sandbox_client.SandboxManager()
        for method, path, kwargs in (
            ("GET", "/v1/storage/objects/stat", {"query": {"scope": "agent"}}),
            ("POST", "/v1/storage/tickets", {"payload": {"scope": "agent"}}),
            ("DELETE", "/v1/storage/objects", {"query": {"scope": "agent"}}),
            (
                "POST",
                "/v1/workspaces/ws-aaaaaaaaaaaa/objects/import",
                {"payload": {"scope": "upload"}},
            ),
        ):
            with self.subTest(path=path):
                with echo_server():
                    with self.assertRaises(RuntimeError) as raised:
                        manager._request(method, path, **kwargs)
                # The operation, so the message points at a call site rather
                # than at the class of problem.
                self.assertIn(path, str(raised.exception))
                self.assertIn("acting_subject_context", str(raised.exception))

    def test_a_management_plane_call_naming_an_owner_needs_no_subject(self) -> None:
        """The identity that has no tenant of its own is not an exception here.

        It names the owner outright, because that is the only way it can act
        for one, and a call that already names a partition needs no subject to
        build one from.
        """
        manager = sandbox_client.SandboxManager()
        with echo_server():
            result, _ = manager._request(
                "POST",
                "/v1/storage/tickets",
                payload={"scope": "agent", "owner": "legacy-host/alice"},
            )
            listed, _ = manager._request(
                "GET",
                "/v1/storage/objects/list",
                query={"scope": "agent", "owner": "legacy-host/alice"},
            )
        self.assertIsNone(result["seen"])
        self.assertIsNone(listed["seen"])

    def test_a_non_object_route_is_unaffected(self) -> None:
        manager = sandbox_client.SandboxManager()
        with echo_server():
            result, _ = manager._request("POST", "/v1/workspaces", payload={})
        self.assertIsNone(result["seen"])


if __name__ == "__main__":
    unittest.main()
