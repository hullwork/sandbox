#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="${SANDBOX_STATE_DIR:-$REPO_ROOT/.sandbox}"
LOG_DIR="$STATE_DIR/logs/verify"
SUMMARY="$STATE_DIR/verify-summary.json"
STARTED_EPOCH="$(date +%s)"
STATUS=failed
CURRENT_PHASE=initialization
PHASE_RESULTS=()
mkdir -p "$LOG_DIR"

write_summary() {
  local ended_epoch="$1"
  VERIFY_STATUS="$STATUS" VERIFY_STARTED="$STARTED_EPOCH" \
  VERIFY_ENDED="$ended_epoch" VERIFY_FAILED_PHASE="$CURRENT_PHASE" \
    python3 - "$SUMMARY" "${PHASE_RESULTS[@]}" <<'PY'
import datetime
import json
import os
import pathlib
import sys

started = int(os.environ["VERIFY_STARTED"])
ended = int(os.environ["VERIFY_ENDED"])
status = os.environ["VERIFY_STATUS"]
phases = {}
for item in sys.argv[2:]:
    name, duration = item.rsplit("=", 1)
    phases[name] = int(duration)
payload = {
    "schema_version": 1,
    "status": status,
    "started_at": datetime.datetime.fromtimestamp(
        started, datetime.timezone.utc
    ).isoformat(),
    "duration_seconds": ended - started,
    "manual_interventions_required": 0 if status == "passed" else None,
    "failed_phase": None if status == "passed" else os.environ["VERIFY_FAILED_PHASE"],
    "phases_seconds": phases,
}
path = pathlib.Path(sys.argv[1])
temporary = path.with_suffix(".json.tmp")
temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
temporary.replace(path)
PY
}

finish() {
  local ended_epoch="$(date +%s)"
  write_summary "$ended_epoch"
  printf '\nVerify %s in %ss\nKPI: %s\n' \
    "$STATUS" "$((ended_epoch - STARTED_EPOCH))" "$SUMMARY"
}
trap finish EXIT

run_phase() {
  local name="$1"
  shift
  local started="$SECONDS"
  local log="$LOG_DIR/$name.log"
  CURRENT_PHASE="$name"
  printf '==> %-20s (log: %s)\n' "$name" "$log"
  set +e
  (set -e; "$@") >"$log" 2>&1
  local result=$?
  set -e
  local duration="$((SECONDS - started))"
  PHASE_RESULTS+=("$name=$duration")
  if [ "$result" -eq 0 ]; then
    printf '<== %-20s passed (%ss)\n' "$name" "$duration"
  else
    printf '<== %-20s failed (%ss); last 100 log lines follow\n' \
      "$name" "$duration" >&2
    tail -n 100 "$log" >&2
    return "$result"
  fi
}

verify_manifests() {
  make -C "$REPO_ROOT" --no-print-directory verify-manifests
}

verify_console() {
  npm --prefix "$REPO_ROOT/console" ci --ignore-scripts
  npm --prefix "$REPO_ROOT/console" run test:i18n
  npm --prefix "$REPO_ROOT/console" run lint
  npm --prefix "$REPO_ROOT/console" run typecheck
  npm --prefix "$REPO_ROOT/console" run build
}

verify_wheel() {
  local artifact_dir
  artifact_dir="$(mktemp -d "${TMPDIR:-/tmp}/sandbox-wheel.XXXXXX")"
  python3 -m pip wheel --disable-pip-version-check --no-deps \
    "$REPO_ROOT" -w "$artifact_dir"
  python3 -m venv "$artifact_dir/venv"
  "$artifact_dir/venv/bin/python" -m pip install --disable-pip-version-check \
    "$artifact_dir"/*.whl
  "$artifact_dir/venv/bin/python" "$REPO_ROOT/scripts/check-wheel-surface.py" \
    "$artifact_dir"/*.whl
  "$artifact_dir/venv/bin/sandbox" --help >/dev/null
  "$artifact_dir/venv/bin/sandboxctl" --help >/dev/null
  "$artifact_dir/venv/bin/sandbox-mcp" --help >/dev/null
  case "$artifact_dir" in
    "${TMPDIR:-/tmp}"/sandbox-wheel.*) rm -rf "$artifact_dir" ;;
    *) echo "refusing to remove unexpected temporary path: $artifact_dir" >&2; return 1 ;;
  esac
}

run_phase python-environment make -C "$REPO_ROOT" --no-print-directory bootstrap
run_phase unit-contracts make -C "$REPO_ROOT" --no-print-directory test
run_phase manifests verify_manifests
run_phase console verify_console
run_phase wheel verify_wheel
STATUS=passed
CURRENT_PHASE=complete
