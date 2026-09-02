#!/usr/bin/env python3
"""Measure an already-running Sandbox Platform without inventing results.

The runner writes each completed operation to ``samples.jsonl`` immediately.
Summary and environment files are derived from those observations only; failed
requests remain failed samples and are never discarded from success rates.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_THRESHOLDS = ROOT / "bench/excellent-thresholds.json"
PROTOCOL_VERSION = "2026-07-28"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def metric_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(sample["seconds"]) for sample in samples if sample["ok"]]
    successes = len(durations)
    count = len(samples)
    return {
        "count": count,
        "successes": successes,
        "successRate": successes / count if count else 0.0,
        "p50Seconds": percentile(durations, 0.50),
        "p95Seconds": percentile(durations, 0.95),
        "p99Seconds": percentile(durations, 0.99),
        "maxSeconds": max(durations) if durations else None,
    }


def evaluate_thresholds(
    summaries: dict[str, dict[str, Any]],
    thresholds: dict[str, Any],
    iterations: int,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    minimum_iterations = thresholds["global"]["minimumRecordedIterations"]
    checks.append({
        "name": "minimumRecordedIterations",
        "actual": iterations,
        "expected": minimum_iterations,
        "passed": iterations >= minimum_iterations,
    })
    total = sum(item["count"] for item in summaries.values())
    successful = sum(item["successes"] for item in summaries.values())
    global_rate = successful / total if total else 0.0
    minimum_global_rate = thresholds["global"]["minimumOperationSuccessRate"]
    checks.append({
        "name": "minimumOperationSuccessRate",
        "actual": global_rate,
        "expected": minimum_global_rate,
        "passed": global_rate >= minimum_global_rate,
    })
    for metric, expected in thresholds["metrics"].items():
        actual = summaries.get(metric)
        if actual is None:
            checks.append({
                "name": f"{metric}.present",
                "actual": False,
                "expected": True,
                "passed": False,
            })
            continue
        checks.append({
            "name": f"{metric}.recordedIterations",
            "actual": actual["count"],
            "expected": iterations,
            "passed": actual["count"] == iterations,
        })
        for field, limit in expected.items():
            value = actual.get(field)
            if field == "minimumSuccessRate":
                value = actual["successRate"]
                passed = value >= limit
            else:
                passed = value is not None and value <= limit
            checks.append({
                "name": f"{metric}.{field}",
                "actual": value,
                "expected": limit,
                "passed": passed,
            })
    return {"excellent": all(check["passed"] for check in checks), "checks": checks}


class ApiClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 120,
    ) -> tuple[int, Any]:
        body = None
        request_headers = {"Authorization": f"Bearer {token or self.token}"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, headers=request_headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            raw = error.read()
            status = error.code
        decoded = json.loads(raw) if raw else {}
        return status, decoded


def command_output(command: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command, text=True, capture_output=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"available": False, "error": type(error).__name__}
    output = completed.stdout.strip() or completed.stderr.strip()
    return {
        "available": completed.returncode == 0,
        "returnCode": completed.returncode,
        "output": output[:20_000],
    }


def environment_snapshot(base_url: str, kube_context: str | None) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(base_url)
    snapshot: dict[str, Any] = {
        "schemaVersion": 1,
        "capturedAt": utc_now(),
        "sourceCommit": command_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
        "sourceStatus": command_output(["git", "-C", str(ROOT), "status", "--porcelain"]),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
        "endpoint": {"scheme": parsed.scheme, "hostname": parsed.hostname, "port": parsed.port},
        "kubeContext": kube_context,
    }
    if kube_context:
        snapshot["kubernetesVersion"] = command_output(
            ["kubectl", "--context", kube_context, "version", "-o", "json"]
        )
        snapshot["runtimeClass"] = command_output(
            ["kubectl", "--context", kube_context, "get", "runtimeclass", "gvisor", "-o", "json"]
        )
        snapshot["systemPods"] = command_output(
            ["kubectl", "--context", kube_context, "-n", "sandbox-system", "get", "pods", "-o", "json"]
        )
        snapshot["workloadPods"] = command_output(
            ["kubectl", "--context", kube_context, "-n", "sandbox-workloads", "get", "pods", "-o", "json"]
        )
    return snapshot


class BenchmarkRun:
    def __init__(self, client: ApiClient, sample_file: pathlib.Path, run_id: str) -> None:
        self.client = client
        self.sample_file = sample_file
        self.run_id = run_id
        self.samples: list[dict[str, Any]] = []

    def measure(self, metric: str, iteration: int, operation: Any) -> Any:
        started = time.perf_counter()
        error = None
        result = None
        try:
            result = operation()
        except Exception as exception:  # the failed observation must be recorded
            error = f"{type(exception).__name__}: {exception}"
        sample = {
            "schemaVersion": 1,
            "runId": self.run_id,
            "recordedAt": utc_now(),
            "metric": metric,
            "iteration": iteration,
            "ok": error is None,
            "seconds": time.perf_counter() - started,
        }
        if error:
            sample["error"] = error[:500]
        self.samples.append(sample)
        with self.sample_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, sort_keys=True) + "\n")
        if error:
            raise RuntimeError(error)
        return result

    @staticmethod
    def require(status: int, body: Any, expected: int) -> Any:
        if status != expected:
            raise RuntimeError(f"expected HTTP {expected}, received {status}")
        return body

    @staticmethod
    def require_file_content(status: int, body: Any, expected_content: str) -> Any:
        body = BenchmarkRun.require(status, body, 200)
        if not isinstance(body, dict) or body.get("content") != expected_content:
            raise RuntimeError("file read returned content with a different digest")
        return body

    @staticmethod
    def require_exec_output(status: int, body: Any, expected_output: str) -> Any:
        body = BenchmarkRun.require(status, body, 200)
        try:
            result = body["result"]["structuredContent"]
        except (KeyError, TypeError):
            raise RuntimeError("shell response did not contain structuredContent") from None
        if result.get("exit_code") != 0 or result.get("stdout") != expected_output:
            raise RuntimeError("shell response did not contain the expected output")
        return body

    def cleanup_workspace(self, workspace_id: str, timeout: float = 30.0) -> None:
        """Purge a workspace only after asynchronous Runtime deletion converges.

        The DELETE sandbox API acknowledges the release before Kubernetes has
        necessarily removed the Pod. A workspace purge during that interval is
        correctly rejected. Benchmarks must retry and verify cleanup instead of
        silently filling the platform's workspace quota.
        """
        deadline = time.monotonic() + timeout
        last_status = 0
        last_body: Any = None
        while time.monotonic() < deadline:
            last_status, last_body = self.client.request(
                "DELETE", f"/v1/workspaces/{workspace_id}?purge=true"
            )
            if last_status in {200, 404}:
                return
            if last_status not in {400, 409}:
                break
            time.sleep(0.25)
        raise RuntimeError(
            f"workspace cleanup failed with HTTP {last_status}: {last_body!r}"
        )

    def one_iteration(self, iteration: int) -> None:
        workspace_id = ""
        workspace_token = ""
        sandbox_id = ""
        sandbox_token = ""
        session = f"bench-{self.run_id}-{iteration}-{uuid.uuid4().hex[:8]}"
        try:
            self.measure(
                "health", iteration,
                lambda: self.require(*self.client.request("GET", "/healthz"), 200),
            )
            workspace = self.measure(
                "workspace_create", iteration,
                lambda: self.require(*self.client.request(
                    "POST", "/v1/workspaces", payload={"session_id": session}
                ), 201),
            )
            workspace_id = workspace["workspace_id"]
            workspace_token = workspace["access_token"]
            sandbox = self.measure(
                "runtime_cold_start", iteration,
                lambda: self.require(*self.client.request(
                    "POST", "/v1/sandboxes", payload={"workspace_id": workspace_id}
                ), 201),
            )
            sandbox_id = sandbox["id"]
            sandbox_token = sandbox["access_token"]
            mcp_payload = {
                "jsonrpc": "2.0",
                "id": f"bench-{iteration}",
                "method": "tools/call",
                "params": {"name": "shell", "arguments": {
                    "action": "exec", "command": "printf bench-ready", "timeout_seconds": 10,
                }},
                "_meta": {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION},
            }
            self.measure(
                "warm_exec", iteration,
                lambda: self.require_exec_output(*self.client.request(
                    "POST", f"/v1/sandboxes/{sandbox_id}/mcp", token=sandbox_token,
                    payload=mcp_payload,
                    headers={"Accept": "application/json, text/event-stream",
                             "MCP-Protocol-Version": PROTOCOL_VERSION,
                             "Mcp-Method": "tools/call", "Mcp-Name": "shell"},
                ), "bench-ready"),
            )
            content = "x" * 1024
            self.measure(
                "file_write_1k", iteration,
                lambda: self.require(*self.client.request(
                    "POST", f"/v1/workspaces/{workspace_id}/files/write",
                    token=workspace_token, payload={"path": "bench/payload.txt", "content": content},
                ), 200),
            )
            encoded_path = urllib.parse.quote("bench/payload.txt", safe="")
            self.measure(
                "file_read_1k", iteration,
                lambda: self.require_file_content(*self.client.request(
                    "GET", f"/v1/workspaces/{workspace_id}/files/read?path={encoded_path}",
                    token=workspace_token,
                ), content),
            )
        finally:
            if sandbox_id:
                status, body = self.client.request(
                    "DELETE", f"/v1/sandboxes/{sandbox_id}"
                )
                if status not in {200, 404}:
                    raise RuntimeError(
                        f"sandbox cleanup failed with HTTP {status}: {body!r}"
                    )
            if workspace_id:
                self.cleanup_workspace(workspace_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("SANDBOX_CONTROL_PLANE_URL"))
    parser.add_argument("--token", default=os.getenv("SANDBOX_TOKEN"))
    parser.add_argument("--kube-context", default=os.getenv("SANDBOX_KUBE_CONTEXT"))
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--thresholds", type=pathlib.Path, default=DEFAULT_THRESHOLDS)
    parser.add_argument("--require-excellent", action="store_true")
    args = parser.parse_args()
    if not args.base_url or not args.token:
        parser.error("--base-url/SANDBOX_CONTROL_PLANE_URL and --token/SANDBOX_TOKEN are required")
    if args.iterations < 1 or args.warmups < 0:
        parser.error("iterations must be positive and warmups must be non-negative")
    return args


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    run_id = uuid.uuid4().hex
    started_at = utc_now()
    (args.output_dir / "environment.json").write_text(
        json.dumps(environment_snapshot(args.base_url, args.kube_context), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    client = ApiClient(args.base_url, args.token)
    samples_path = args.output_dir / "samples.jsonl"
    benchmark = BenchmarkRun(client, samples_path, run_id)
    warmup = BenchmarkRun(client, args.output_dir / "warmup-samples.jsonl", f"{run_id}-warmup")
    failures = 0
    for index in range(args.warmups):
        try:
            warmup.one_iteration(index)
        except Exception as error:
            print(f"warmup {index} failed: {error}", file=sys.stderr)
            return 2
    for index in range(args.iterations):
        try:
            benchmark.one_iteration(index)
        except Exception as error:
            failures += 1
            print(f"iteration {index} failed: {error}", file=sys.stderr)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sample in benchmark.samples:
        grouped.setdefault(sample["metric"], []).append(sample)
    summaries = {name: metric_summary(items) for name, items in sorted(grouped.items())}
    thresholds = json.loads(args.thresholds.read_text(encoding="utf-8"))
    evaluation = evaluate_thresholds(summaries, thresholds, args.iterations)
    summary = {
        "schemaVersion": 1,
        "runId": run_id,
        "startedAt": started_at,
        "finishedAt": utc_now(),
        "iterations": args.iterations,
        "iterationFailures": failures,
        "thresholdProfile": thresholds["profile"],
        "metrics": summaries,
        "evaluation": evaluation,
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, sort_keys=True))
    if failures:
        return 1
    if args.require_excellent and not evaluation["excellent"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
