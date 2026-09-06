from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from bench import runner
from bench.runner import (
    BenchmarkRun,
    command_output,
    evaluate_thresholds,
    metric_summary,
    percentile,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


class BenchmarkMathTests(unittest.TestCase):
    def test_percentile_interpolates_and_never_discards_failures_from_rate(self) -> None:
        self.assertEqual(percentile([1.0, 2.0, 3.0], 0.5), 2.0)
        summary = metric_summary([
            {"ok": True, "seconds": 0.1},
            {"ok": False, "seconds": 0.2},
        ])
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["successes"], 1)
        self.assertEqual(summary["successRate"], 0.5)

    def test_missing_metric_and_short_run_fail_excellent_evaluation(self) -> None:
        thresholds = json.loads(
            (ROOT / "bench/excellent-thresholds.json").read_text(encoding="utf-8")
        )
        evaluation = evaluate_thresholds({}, thresholds, iterations=1)
        self.assertFalse(evaluation["excellent"])
        names = {check["name"] for check in evaluation["checks"] if not check["passed"]}
        self.assertIn("minimumRecordedIterations", names)
        self.assertIn("runtime_cold_start.present", names)

    def test_partial_metric_samples_cannot_be_excellent(self) -> None:
        thresholds = json.loads(
            (ROOT / "bench/excellent-thresholds.json").read_text(encoding="utf-8")
        )
        summaries = {
            metric: metric_summary([
                {"ok": True, "seconds": 0.01}
                for _ in range(99 if metric == "warm_exec" else 100)
            ])
            for metric in thresholds["metrics"]
        }
        evaluation = evaluate_thresholds(summaries, thresholds, iterations=100)
        self.assertFalse(evaluation["excellent"])
        failed = {check["name"] for check in evaluation["checks"] if not check["passed"]}
        self.assertIn("warm_exec.recordedIterations", failed)

    def test_environment_capture_does_not_name_token_inputs(self) -> None:
        source = (ROOT / "bench/runner.py").read_text(encoding="utf-8")
        function = source.split("def environment_snapshot", 1)[1].split("class BenchmarkRun", 1)[0]
        self.assertNotIn("SANDBOX_TOKEN", function)
        self.assertNotIn('"token"', function)

    def test_one_iteration_records_only_verified_operations_and_cleans_up(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def request(self, method: str, path: str, **kwargs: object) -> tuple[int, object]:
                self.calls.append((method, path))
                if path == "/healthz":
                    return 200, {"status": "ok"}
                if method == "POST" and path == "/v1/workspaces":
                    return 201, {"workspace_id": "ws-test", "access_token": "workspace-token"}
                if method == "POST" and path == "/v1/sandboxes":
                    return 201, {"id": "sb-test", "access_token": "sandbox-token"}
                if path.endswith("/mcp"):
                    return 200, {"jsonrpc": "2.0", "result": {"structuredContent": {
                        "exit_code": 0, "stdout": "bench-ready",
                    }}}
                if path.endswith("/files/write"):
                    return 200, {"ok": True}
                if "/files/read?" in path:
                    return 200, {"content": "x" * 1024}
                if method == "DELETE":
                    return 200, {"ok": True}
                raise AssertionError((method, path, kwargs))

        with tempfile.TemporaryDirectory() as directory:
            samples = pathlib.Path(directory) / "samples.jsonl"
            client = FakeClient()
            run = BenchmarkRun(client, samples, "unit")  # type: ignore[arg-type]
            run.one_iteration(0)
            recorded = [json.loads(line) for line in samples.read_text().splitlines()]

        self.assertEqual(len(recorded), 6)
        self.assertTrue(all(item["ok"] for item in recorded))
        self.assertIn(("DELETE", "/v1/sandboxes/sb-test"), client.calls)
        self.assertIn(("DELETE", "/v1/workspaces/ws-test?purge=true"), client.calls)

    def test_the_runtime_class_that_ran_is_taken_from_the_lease(self) -> None:
        """What the Pods ran under is an observation, not a cluster fact.

        ``environment.json`` dumps the ``gvisor`` RuntimeClass object, and it is
        written before the first Runtime exists. A deployment that leaves
        ``SANDBOX_RUNTIME_CLASS`` empty has that object and still places every
        Pod on the cluster default runtime, so the snapshot alone would let a
        report read as a gVisor measurement when it is not one. Each lease says
        which class it got; the summary reports those.
        """
        class FakeClient:
            def request(self, method: str, path: str, **kwargs: object) -> tuple[int, object]:
                if path == "/healthz":
                    return 200, {"status": "ok"}
                if method == "POST" and path == "/v1/workspaces":
                    return 201, {"workspace_id": "ws-test", "access_token": "workspace-token"}
                if method == "POST" and path == "/v1/sandboxes":
                    return 201, {
                        "id": "sb-test",
                        "access_token": "sandbox-token",
                        "runtime_class": "cluster-default",
                    }
                if path.endswith("/mcp"):
                    return 200, {"jsonrpc": "2.0", "result": {"structuredContent": {
                        "exit_code": 0, "stdout": "bench-ready",
                    }}}
                if path.endswith("/files/write"):
                    return 200, {"ok": True}
                if "/files/read?" in path:
                    return 200, {"content": "x" * 1024}
                if method == "DELETE":
                    return 200, {"ok": True}
                raise AssertionError((method, path, kwargs))

        with tempfile.TemporaryDirectory() as directory:
            run = BenchmarkRun(  # type: ignore[arg-type]
                FakeClient(), pathlib.Path(directory) / "samples.jsonl", "unit"
            )
            run.one_iteration(0)
        self.assertEqual(run.runtime_classes, {"cluster-default"})

    def test_a_lease_without_the_field_is_recorded_as_unreported(self) -> None:
        # A Control Plane too old to send runtime_class is not evidence of
        # gVisor and not evidence against it. It must not become "gvisor" by
        # omission, and it must not crash the summary's sort either.
        class FakeClient:
            def request(self, method: str, path: str, **kwargs: object) -> tuple[int, object]:
                if path == "/healthz":
                    return 200, {"status": "ok"}
                if method == "POST" and path == "/v1/workspaces":
                    return 201, {"workspace_id": "ws-test", "access_token": "workspace-token"}
                if method == "POST" and path == "/v1/sandboxes":
                    return 201, {"id": "sb-test", "access_token": "sandbox-token"}
                if path.endswith("/mcp"):
                    return 200, {"jsonrpc": "2.0", "result": {"structuredContent": {
                        "exit_code": 0, "stdout": "bench-ready",
                    }}}
                if path.endswith("/files/write"):
                    return 200, {"ok": True}
                if "/files/read?" in path:
                    return 200, {"content": "x" * 1024}
                if method == "DELETE":
                    return 200, {"ok": True}
                raise AssertionError((method, path, kwargs))

        with tempfile.TemporaryDirectory() as directory:
            run = BenchmarkRun(  # type: ignore[arg-type]
                FakeClient(), pathlib.Path(directory) / "samples.jsonl", "unit"
            )
            run.one_iteration(0)
        self.assertEqual(run.runtime_classes, {"unreported"})
        self.assertEqual(sorted(run.runtime_classes), ["unreported"])

    def test_truncated_command_output_says_so(self) -> None:
        # A `kubectl get pods -o json` past the cap lands in the report as a
        # JSON document that stops mid-object. Without the marker the reader
        # cannot tell that from a command that failed halfway.
        long_output = command_output(
            [sys.executable, "-c", f"print('x' * {runner.OUTPUT_CAP + 500})"]
        )
        self.assertTrue(long_output["truncated"])
        self.assertEqual(long_output["fullLength"], runner.OUTPUT_CAP + 500)
        self.assertEqual(len(long_output["output"]), runner.OUTPUT_CAP)

        short_output = command_output([sys.executable, "-c", "print('ok')"])
        self.assertNotIn("truncated", short_output)
        self.assertEqual(short_output["output"], "ok")

    def test_cleanup_waits_for_asynchronous_runtime_deletion(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.statuses = [409, 400, 200]

            def request(self, method: str, path: str, **kwargs: object) -> tuple[int, object]:
                self.assert_cleanup_request(method, path)
                status = self.statuses.pop(0)
                return status, {"status": status}

            @staticmethod
            def assert_cleanup_request(method: str, path: str) -> None:
                if method != "DELETE" or path != "/v1/workspaces/ws-test?purge=true":
                    raise AssertionError((method, path))

        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "bench.runner.time.sleep"
        ) as sleep:
            run = BenchmarkRun(  # type: ignore[arg-type]
                client, pathlib.Path(directory) / "samples.jsonl", "unit"
            )
            run.cleanup_workspace("ws-test")
        self.assertEqual(client.statuses, [])
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
