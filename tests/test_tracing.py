"""``traceparent`` propagation, in and out.

Two halves, because one is not evidence for the other:

* the pure rules (shape validation, the order of preference, derivation) are
  checked directly;
* the wiring is checked against a running Control Plane with a **real** downstream
  server on the other side, so "the header is attached to outbound calls" is
  observed on the wire rather than asserted about a stub.

🔴 The case that matters most is the malformed one. A trace header is a
diagnostic aid, and a request that fails because its trace header was wrong
turns the diagnostic layer into a source of outages. Every malformed input here
is asserted to be **served normally**, with a locally generated id - not
rejected, and not answered with an empty or invalid trace id either.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from control_plane import tracing  # noqa: E402


TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
VALID = f"00-{TRACE_ID}-{SPAN_ID}-01"
UNSAMPLED = f"00-{TRACE_ID}-{SPAN_ID}-00"
HEX32 = re.compile(r"^[0-9a-f]{32}$")


class ShapeTests(unittest.TestCase):
    """What counts as a usable ``traceparent``."""

    def test_a_well_formed_header_yields_its_trace_id_and_flags(self) -> None:
        self.assertEqual(tracing.parse_traceparent(VALID), (TRACE_ID, "01"))
        self.assertEqual(tracing.parse_traceparent(f"  {VALID}  "), (TRACE_ID, "01"))
        self.assertEqual(
            tracing.parse_traceparent(f"00-{TRACE_ID}-{SPAN_ID}-00"),
            (TRACE_ID, "00"),
        )

    def test_every_malformed_shape_is_treated_as_absent(self) -> None:
        for label, value in {
            "future version": f"01-{TRACE_ID}-{SPAN_ID}-01",
            "no version": f"{TRACE_ID}-{SPAN_ID}-01",
            "too few segments": f"00-{TRACE_ID}-{SPAN_ID}",
            "too many segments": f"00-{TRACE_ID}-{SPAN_ID}-01-extra",
            "short trace id": f"00-{TRACE_ID[:-1]}-{SPAN_ID}-01",
            "long trace id": f"00-{TRACE_ID}a-{SPAN_ID}-01",
            "non hex trace id": f"00-{'g' * 32}-{SPAN_ID}-01",
            "uppercase trace id": f"00-{TRACE_ID.upper()}-{SPAN_ID}-01",
            "short span id": f"00-{TRACE_ID}-{SPAN_ID[:-1]}-01",
            "non hex flags": f"00-{TRACE_ID}-{SPAN_ID}-zz",
            "all-zero trace id": f"00-{'0' * 32}-{SPAN_ID}-01",
            "all-zero span id": f"00-{TRACE_ID}-{'0' * 16}-01",
            "empty": "",
            "not a string": None,
        }.items():
            with self.subTest(case=label):
                self.assertIsNone(tracing.parse_traceparent(value))

    def test_the_zero_ids_are_refused_for_a_reason(self) -> None:
        # They are what a broken producer emits, and accepting one would put
        # every such request into a single shared trace - worse than no trace.
        self.assertIsNone(tracing.parse_traceparent(f"00-{'0' * 32}-{SPAN_ID}-01"))
        self.assertIsNone(tracing.parse_traceparent(f"00-{TRACE_ID}-{'0' * 16}-01"))


class SamplingFlagTests(unittest.TestCase):
    """🔴 The flags belong to whoever started the trace, not to this hop.

    Overwriting them reverses a sampling decision somebody already made, and
    reverses it invisibly: the trace stays connected, every other assertion
    here still passes, and the only symptom is a change in volume at the
    collector that cannot be attributed to any hop. So the rule is narrow -
    inherit whenever there was an inbound decision, decide only when there was
    none.
    """

    def test_an_upstream_decision_is_carried_through_untouched(self) -> None:
        for flags in ("00", "01", "ff"):
            with self.subTest(flags=flags):
                trace_id, parsed = tracing.parse_traceparent(
                    f"00-{TRACE_ID}-{SPAN_ID}-{flags}"
                )
                self.assertEqual(parsed, flags)
                self.assertTrue(
                    tracing.traceparent(trace_id, parsed).endswith(f"-{flags}")
                )

    def test_declining_to_sample_is_a_decision_too(self) -> None:
        # The case that a blanket "01" would silently overturn.
        trace_id, flags = tracing.inbound_trace(
            {"traceparent": f"00-{TRACE_ID}-{SPAN_ID}-00"}
        )
        self.assertEqual((trace_id, flags), (TRACE_ID, "00"))
        self.assertTrue(tracing.traceparent(trace_id, flags).endswith("-00"))

    def test_a_trace_we_start_ourselves_is_marked_sampled(self) -> None:
        # No upstream decision exists, and "00" would have a tracing-aware
        # gateway discard the span we just started.
        for headers in ({}, {"X-Request-Id": "an-id"}, {"traceparent": "garbage"}):
            with self.subTest(headers=headers):
                self.assertEqual(tracing.inbound_trace(headers)[1], "01")


class GenerationTests(unittest.TestCase):
    def test_generated_ids_are_well_formed_and_never_zero(self) -> None:
        traces = {tracing.new_trace_id() for _ in range(200)}
        self.assertEqual(len(traces), 200)
        for trace_id in traces:
            self.assertRegex(trace_id, HEX32)
            self.assertNotEqual(trace_id, "0" * 32)
        spans = {tracing.new_span_id() for _ in range(200)}
        self.assertEqual(len(spans), 200)
        for span_id in spans:
            self.assertRegex(span_id, r"^[0-9a-f]{16}$")
            self.assertNotEqual(span_id, "0" * 16)

    def test_a_generated_id_survives_its_own_parser(self) -> None:
        # The producer and the consumer of this header are the same codebase on
        # different hops; a generated header this parser would reject would be a
        # trace that breaks at our own boundary.
        for _ in range(50):
            header = tracing.traceparent(tracing.new_trace_id())
            self.assertIsNotNone(tracing.parse_traceparent(header))


class DerivationTests(unittest.TestCase):
    def test_a_request_id_derives_the_same_trace_id_every_time(self) -> None:
        first = tracing.derive_trace_id("a-request-id")
        self.assertEqual(first, tracing.derive_trace_id("a-request-id"))
        self.assertRegex(first, HEX32)

    def test_different_request_ids_derive_different_trace_ids(self) -> None:
        self.assertNotEqual(
            tracing.derive_trace_id("one"), tracing.derive_trace_id("two")
        )

    def test_the_derivation_is_the_agreed_one(self) -> None:
        # Fixed rule, not local state: the point is that two services handed the
        # same request id independently arrive at the same trace id.
        import hashlib

        expected = hashlib.sha256(b"a-request-id").digest()[:16].hex()
        self.assertEqual(tracing.derive_trace_id("a-request-id"), expected)


class PreferenceOrderTests(unittest.TestCase):
    def test_a_valid_traceparent_wins_over_a_request_id(self) -> None:
        self.assertEqual(
            tracing.inbound_trace_id(
                {"traceparent": VALID, "X-Request-Id": "ignored"}
            ),
            TRACE_ID,
        )

    def test_a_malformed_traceparent_falls_through_to_the_request_id(self) -> None:
        self.assertEqual(
            tracing.inbound_trace_id(
                {"traceparent": "garbage", "X-Request-Id": "a-request-id"}
            ),
            tracing.derive_trace_id("a-request-id"),
        )

    def test_nothing_at_all_still_produces_a_usable_id(self) -> None:
        generated = tracing.inbound_trace_id({})
        self.assertRegex(generated, HEX32)
        self.assertNotEqual(generated, "0" * 32)

    def test_a_blank_request_id_is_not_a_request_id(self) -> None:
        first = tracing.inbound_trace_id({"X-Request-Id": "   "})
        second = tracing.inbound_trace_id({"X-Request-Id": "   "})
        self.assertRegex(first, HEX32)
        self.assertNotEqual(first, second, "a blank value must not derive an id")


class OutboundHeaderTests(unittest.TestCase):
    def test_the_span_id_is_new_on_every_hop(self) -> None:
        spans = {tracing.traceparent(TRACE_ID).split("-")[2] for _ in range(100)}
        self.assertEqual(len(spans), 100)

    def test_the_trace_id_is_carried_through_unchanged(self) -> None:
        self.assertEqual(tracing.traceparent(TRACE_ID).split("-")[1], TRACE_ID)

    def test_outside_a_request_nothing_is_emitted(self) -> None:
        # Background work must not claim to belong to a trace nobody started.
        tracing.set_current("")
        self.assertEqual(tracing.outbound_headers(), {})

    def test_an_active_client_span_is_the_outbound_parent(self) -> None:
        tracing.set_current(TRACE_ID, "01", SPAN_ID)
        with mock.patch.object(tracing, "_OTLP_ENDPOINT", ""):
            with tracing.start_span("client", kind=3) as span:
                self.assertEqual(
                    tracing.outbound_headers()["traceparent"].split("-")[2],
                    span.span_id,
                )


class OtlpSpanTests(unittest.TestCase):
    def setUp(self) -> None:
        while True:
            try:
                tracing._OTLP_QUEUE.get_nowait()
                tracing._OTLP_QUEUE.task_done()
            except Exception:
                break

    def test_sampled_span_is_valid_otlp_json_data(self) -> None:
        tracing.set_current(TRACE_ID, "01", SPAN_ID)
        with (
            mock.patch.object(tracing, "_OTLP_ENDPOINT", "http://collector/v1/traces"),
            mock.patch.object(tracing, "_ensure_exporter"),
        ):
            with tracing.start_span(
                "runtime.create.pod_ready",
                attributes={"sandbox.runtime.phase": "pod_ready"},
            ):
                pass
        encoded = tracing._OTLP_QUEUE.get_nowait()
        tracing._OTLP_QUEUE.task_done()
        self.assertEqual(encoded["traceId"], TRACE_ID)
        self.assertEqual(encoded["parentSpanId"], SPAN_ID)
        self.assertRegex(encoded["spanId"], r"^[0-9a-f]{16}$")
        self.assertIsInstance(encoded["startTimeUnixNano"], str)
        self.assertEqual(encoded["status"]["code"], 1)

    def test_unsampled_trace_is_propagated_but_not_exported(self) -> None:
        tracing.set_current(TRACE_ID, "00", SPAN_ID)
        with (
            mock.patch.object(tracing, "_OTLP_ENDPOINT", "http://collector/v1/traces"),
            mock.patch.object(tracing, "_ensure_exporter"),
        ):
            with tracing.start_span("not-exported"):
                self.assertTrue(tracing.outbound_headers()["traceparent"].endswith("-00"))
        self.assertTrue(tracing._OTLP_QUEUE.empty())


class PublishedContractTests(unittest.TestCase):
    """The half of this contract that only a document can carry.

    Header-name casing is deliberately **not** guaranteed - HTTP field names
    are case-insensitive and each service sends what its client produces. The
    obligation that replaces it falls on receivers, and a receiver reads the
    document, not this test suite. If the sentence disappears the obligation
    disappears with it, silently, so it is pinned here for the same reason the
    behaviour is.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.published = (ROOT / "docs/API.md").read_text(encoding="utf-8")

    def test_the_casing_rule_is_published_in_both_halves(self) -> None:
        self.assertIn("not part of this contract", self.published)
        self.assertIn("case-insensitively", self.published)

    def test_the_flag_inheritance_rule_is_published(self) -> None:
        self.assertIn("inherited unchanged", self.published)

    def test_the_untraced_hop_is_admitted(self) -> None:
        # A gap somebody will find. Saying where the trace stops is what keeps
        # them from suspecting their own query first. Each document is checked
        # against the words it actually uses rather than a normalized form -
        # the point is that a reader of *that* page is told.
        for relative, phrase in (
            ("docs/API.md", "no place to inject a header"),
            ("README.md", "no header injection point"),
        ):
            with self.subTest(document=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("`mc`", text)
                self.assertIn(phrase, text)


PROBE = textwrap.dedent(
    """
    import io
    import json
    import sys
    import threading
    import urllib.error
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from control_plane import kube
    class FakeKube:
        def __init__(self):
            pass

        def list(self, namespace, plural, *, label_selector=None):
            return []

    kube.KubeClient = FakeKube

    DOWNSTREAM = {"seen": []}

    class Downstream(BaseHTTPRequestHandler):
        # A real server on the other end, so outbound headers are observed on
        # the wire instead of asserted about a stub.

        def do_GET(self):
            # Read the way HTTP defines it - field names are case-insensitive
            # (RFC 9110) - and keep the raw casing too, so the test can say
            # what actually went on the wire rather than assume.
            DOWNSTREAM["seen"].append({
                "traceparent": self.headers.get("traceparent"),
                "names": [name for name, _ in self.headers.items()],
            })
            body = b'{"workspaces": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    downstream = ThreadingHTTPServer(("127.0.0.1", 0), Downstream)
    threading.Thread(target=downstream.serve_forever, daemon=True).start()

    import os
    os.environ["VOLUME_AGENT_URL"] = f"http://127.0.0.1:{downstream.server_port}"

    from control_plane import api
    from control_plane import core as control_plane
    control_plane.VOLUME_AGENT_URL = os.environ["VOLUME_AGENT_URL"]

    server = ThreadingHTTPServer(("127.0.0.1", 0), api.ApiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    results = {}

    def call(name, path, headers=None):
        before_logs = len(LOG.getvalue().splitlines())
        before_seen = len(DOWNSTREAM["seen"])
        request = urllib.request.Request(f"{base}{path}", method="GET")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
                echoed = response.headers.get("X-Request-Id")
        except urllib.error.HTTPError as exc:
            status, echoed = exc.code, exc.headers.get("X-Request-Id")
        logged = [
            line for line in LOG.getvalue().splitlines()[before_logs:]
            if "trace_id=" in line
        ]
        results[name] = {
            "status": status,
            "echoed": echoed,
            "logged": [line.rsplit("trace_id=", 1)[1].strip() for line in logged],
            "downstream": DOWNSTREAM["seen"][before_seen:],
        }

    LOG = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = LOG
    try:
        call("valid", "/v1/workspaces", {"traceparent": VALID,
                                         "Authorization": f"Bearer {TOKEN}"})
        call("unsampled", "/v1/workspaces",
             {"traceparent": UNSAMPLED, "Authorization": f"Bearer {TOKEN}"})
        call("generated_outbound", "/v1/workspaces",
             {"Authorization": f"Bearer {TOKEN}"})
        call("nothing", "/livez")
        call("request_id_only", "/livez", {"X-Request-Id": "shared-request-id"})
        call("request_id_again", "/livez", {"X-Request-Id": "shared-request-id"})
        for name, value in MALFORMED.items():
            call(f"bad_{name}", "/livez", {"traceparent": value})
        call("bad_with_request_id", "/livez",
             {"traceparent": "garbage", "X-Request-Id": "shared-request-id"})
    finally:
        sys.stdout = real_stdout
        server.shutdown()
        server.server_close()
        downstream.shutdown()
        downstream.server_close()
    print(json.dumps(results))
    """
)

MALFORMED = {
    "version": f"01-{TRACE_ID}-{SPAN_ID}-01",
    "segments": f"00-{TRACE_ID}-{SPAN_ID}",
    "zero_trace": f"00-{'0' * 32}-{SPAN_ID}-01",
    "non_hex": f"00-{'g' * 32}-{SPAN_ID}-01",
    "empty": "",
}
TOKEN = "test-control-plane-token"


def run_probe() -> dict:
    environment = {
        **os.environ,
        "SANDBOX_CONTROL_PLANE_TOKEN": TOKEN,
        "SIGNING_KEY": "0" * 32,
        "WORKSPACE_ID_KEY": "1" * 32,
        "VOLUME_AGENT_TOKEN": "test-volume-token",
        "OBJECT_STORE_ENDPOINT": "http://127.0.0.1:1",
        "OBJECT_STORE_ACCESS_KEY": "test-access",
        "OBJECT_STORE_SECRET_KEY": "test-secret",
        "PYTHONPATH": str(ROOT),
    }
    environment.pop("SANDBOX_CONTROL_PLANE_ROLE", None)
    for name in list(environment):
        if name.startswith("SANDBOX_CONTROL_PLANE_OIDC_"):
            environment.pop(name)
    source = (
        f"VALID = {VALID!r}\nUNSAMPLED = {UNSAMPLED!r}\n"
        f"TOKEN = {TOKEN!r}\nMALFORMED = {MALFORMED!r}\n"
    ) + PROBE
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class LiveRequestTests(unittest.TestCase):
    """The wiring, against a running Control Plane and a real downstream."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.results = run_probe()

    def one_trace(self, name: str) -> str:
        entry = self.results[name]
        self.assertEqual(len(entry["logged"]), 1, entry)
        return entry["logged"][0]

    def test_an_inbound_trace_id_is_adopted_and_logged(self) -> None:
        self.assertEqual(self.one_trace("valid"), TRACE_ID)

    def test_the_same_trace_id_goes_out_with_a_fresh_span(self) -> None:
        forwarded = self.results["valid"]["downstream"]
        self.assertEqual(len(forwarded), 1, forwarded)
        header = forwarded[0]["traceparent"]
        self.assertIsNotNone(header, forwarded[0])
        version, trace, span, _ = header.split("-")
        self.assertEqual(version, "00")
        self.assertEqual(trace, TRACE_ID, "the trace id must survive the hop")
        self.assertNotEqual(span, SPAN_ID, "each hop needs its own span id")
        self.assertRegex(span, r"^[0-9a-f]{16}$")

    def test_the_header_name_is_sent_case_insensitively_matchable(self) -> None:
        """What the name looks like on the wire, stated rather than assumed.

        urllib capitalizes request header names, so this hop sends
        ``Traceparent``. HTTP field names are case-insensitive, so every
        conformant receiver - including the services on the other side of this
        boundary - matches it. Pinned here because the specification writes the
        name in lowercase, and someone comparing a packet capture against the
        specification should find the discrepancy explained instead of filing
        it as a bug.
        """
        names = self.results["valid"]["downstream"][0]["names"]
        matches = [name for name in names if name.lower() == "traceparent"]
        self.assertEqual(len(matches), 1, names)
        self.assertEqual(matches[0].lower(), "traceparent")

    def test_the_upstream_sampling_decision_survives_the_hop(self) -> None:
        """Observed on the wire, not inferred from the parser."""
        for name, expected in (("valid", "01"), ("unsampled", "00")):
            with self.subTest(case=name):
                header = self.results[name]["downstream"][0]["traceparent"]
                self.assertTrue(
                    header.endswith(f"-{expected}"),
                    f"{name}: sent {header}, expected flags {expected}",
                )
                self.assertEqual(header.split("-")[1], TRACE_ID)

    def test_a_trace_we_started_goes_out_marked_sampled(self) -> None:
        header = self.results["generated_outbound"]["downstream"][0]["traceparent"]
        self.assertTrue(header.endswith("-01"), header)

    def test_the_response_echoes_what_was_logged(self) -> None:
        for name in ("valid", "nothing", "request_id_only"):
            with self.subTest(case=name):
                self.assertEqual(
                    self.results[name]["echoed"], self.one_trace(name)
                )

    def test_a_request_id_alone_derives_the_trace_id_deterministically(self) -> None:
        first = self.one_trace("request_id_only")
        self.assertEqual(first, tracing.derive_trace_id("shared-request-id"))
        self.assertEqual(first, self.one_trace("request_id_again"))

    def test_no_headers_at_all_still_gets_a_usable_id(self) -> None:
        generated = self.one_trace("nothing")
        self.assertRegex(generated, HEX32)
        self.assertNotEqual(generated, "0" * 32)

    def test_a_malformed_header_never_fails_the_request(self) -> None:
        """🔴 The whole point: diagnostics may not decide availability."""
        generated = set()
        for name in MALFORMED:
            with self.subTest(case=name):
                entry = self.results[f"bad_{name}"]
                self.assertEqual(entry["status"], 200, entry)
                trace_id = self.one_trace(f"bad_{name}")
                self.assertRegex(trace_id, HEX32)
                self.assertNotEqual(trace_id, "0" * 32)
                self.assertNotEqual(
                    trace_id, TRACE_ID,
                    "a malformed header must not still be mined for its id",
                )
                generated.add(trace_id)
        self.assertEqual(
            len(generated), len(MALFORMED),
            "each rejected header should have produced its own fresh id",
        )

    def test_a_malformed_header_still_falls_through_to_the_request_id(self) -> None:
        self.assertEqual(
            self.one_trace("bad_with_request_id"),
            tracing.derive_trace_id("shared-request-id"),
        )


if __name__ == "__main__":
    unittest.main()
