#!/usr/bin/env python3
"""One visible, reproducible proof that the local profile delivers its claims."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE_DIR = pathlib.Path(os.environ.get("SANDBOX_STATE_DIR", ROOT / ".sandbox"))
CONTROL_PLANE_URL = os.environ.get(
    "SANDBOX_CONTROL_PLANE_URL", "http://127.0.0.1:18080"
).rstrip("/")


def local_token() -> str:
    existing = os.environ.get("SANDBOX_TOKEN", "").strip()
    if existing:
        return existing
    return subprocess.run(
        ["make", "--no-print-directory", "dev-token"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def get(path: str) -> tuple[bytes, dict[str, str]]:
    with urllib.request.urlopen(f"{CONTROL_PLANE_URL}{path}", timeout=15) as response:
        return response.read(), dict(response.headers.items())


def main() -> int:
    started = time.monotonic()
    token = local_token()
    if not token:
        raise RuntimeError("the local development token is empty")
    os.environ["SANDBOX_CONTROL_PLANE_URL"] = CONTROL_PLANE_URL
    os.environ["SANDBOX_TOKEN"] = token

    # Import only after setting the client environment: its default manager is
    # intentionally configured once, at module import time.
    from sandbox_platform.sandbox_client import Sandbox

    health_raw, headers = get("/healthz")
    health = json.loads(health_raw)
    if health.get("status") != "ok":
        raise RuntimeError(f"health check did not pass: {health}")
    request_id = next(
        (value for key, value in headers.items() if key.lower() == "x-request-id"),
        "",
    )
    if not request_id:
        raise RuntimeError("health response did not carry X-Request-Id")

    proof = f"workspace-persisted-{time.time_ns()}"
    first_call_started = time.monotonic()
    sandbox = Sandbox.get_or_create("quickstart-showcase")
    result = sandbox.run_command(
        "sh",
        [
            "-lc",
            "set -eu; "
            "test \"$(id -u)\" -ne 0; "
            "test ! -e /var/run/secrets/kubernetes.io/serviceaccount/token; "
            "if touch /root/sandbox-should-be-read-only 2>/dev/null; then exit 42; fi; "
            f"printf %s {proof!r} > /workspace/.quickstart-proof; "
            "printf 'kernel=%s uid=%s\\n' \"$(uname -r)\" \"$(id -u)\"",
        ],
    )
    first_call_seconds = time.monotonic() - first_call_started
    if result.exit_code != 0 or "gvisor" not in result.stdout:
        raise RuntimeError(
            f"gVisor isolation proof failed: exit={result.exit_code}, "
            f"stdout={result.stdout!r}, stderr={result.stderr!r}"
        )
    sandbox.stop()

    resumed = Sandbox.get_or_create("quickstart-showcase")
    persisted = resumed.run_command("cat", ["/workspace/.quickstart-proof"])
    resumed.stop()
    if persisted.exit_code != 0 or persisted.stdout != proof:
        raise RuntimeError("workspace did not survive Runtime replacement")

    metrics_raw, _ = get("/metrics")
    if b"sandbox_http_requests_total" not in metrics_raw:
        raise RuntimeError("Prometheus metrics endpoint did not expose request metrics")

    with tempfile.TemporaryDirectory(prefix="sandbox-fail-closed-") as directory:
        marker = pathlib.Path(directory) / "host-command-ran"
        environment = dict(os.environ)
        environment["SANDBOX_CONTROL_PLANE_URL"] = "http://127.0.0.1:9"
        failed = subprocess.run(
            [
                str(ROOT / ".venv/bin/sandbox"),
                "run",
                "--name",
                "fail-closed-proof",
                "--",
                "touch",
                str(marker),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if failed.returncode == 0 or marker.exists():
            raise RuntimeError("Control Plane outage did not fail closed")

    duration = time.monotonic() - started
    result_path = STATE_DIR / "showcase-result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "passed",
                "control_plane_url": CONTROL_PLANE_URL,
                "first_runtime_call_seconds": round(first_call_seconds, 3),
                "total_seconds": round(duration, 3),
                "checks": {
                    "gvisor_kernel": True,
                    "non_root": True,
                    "read_only_root": True,
                    "no_service_account_token": True,
                    "workspace_survives_runtime_replacement": True,
                    "control_plane_outage_fails_closed": True,
                    "request_id": True,
                    "prometheus_metrics": True,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"✓ isolated Runtime: {result.stdout.strip()}")
    print("✓ non-root, read-only root, no Kubernetes service-account token")
    print("✓ Workspace file survived a stopped and replaced Runtime")
    print("✓ Control Plane outage failed closed; no host command ran")
    print("✓ health, X-Request-Id, and Prometheus metrics are observable")
    print(f"First Runtime call: {first_call_seconds:.3f}s")
    print(f"Value proof total: {duration:.3f}s")
    print(f"Evidence: {result_path}")
    print("Console: run `make console-forward`, open http://127.0.0.1:18081,")
    print("then paste the value from `make --no-print-directory dev-token` into API key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
