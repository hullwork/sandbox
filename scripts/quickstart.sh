#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="${SANDBOX_STATE_DIR:-$REPO_ROOT/.sandbox}"
SUMMARY="$STATE_DIR/quickstart-summary.json"
STARTED_EPOCH="$(date +%s)"
STATUS=failed
CURRENT_PHASE=initialization
PROFILE_MODE=created
PHASE_RESULTS=()

if command -v limactl >/dev/null 2>&1 \
  && limactl list --format '{{.Name}}' 2>/dev/null \
    | grep -Fx "${SANDBOX_LOCAL_VM:-sandbox-local}" >/dev/null; then
  PROFILE_MODE=reused
fi

mkdir -p "$STATE_DIR"

write_summary() {
  local ended_epoch="$1"
  QUICKSTART_STATUS="$STATUS" \
  QUICKSTART_STARTED="$STARTED_EPOCH" \
  QUICKSTART_ENDED="$ended_epoch" \
  QUICKSTART_FAILED_PHASE="$CURRENT_PHASE" \
  QUICKSTART_PROFILE_MODE="$PROFILE_MODE" \
  QUICKSTART_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)" \
  QUICKSTART_DIRTY="$(test -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" && echo true || echo false)" \
    python3 - "$SUMMARY" "${PHASE_RESULTS[@]}" <<'PY'
import datetime
import json
import os
import pathlib
import sys

started = int(os.environ["QUICKSTART_STARTED"])
ended = int(os.environ["QUICKSTART_ENDED"])
status = os.environ["QUICKSTART_STATUS"]
phases = {}
for item in sys.argv[2:]:
    name, duration = item.rsplit("=", 1)
    phases[name] = int(duration)
payload = {
    "schema_version": 1,
    "status": status,
    "profile_mode": os.environ["QUICKSTART_PROFILE_MODE"],
    "commit": os.environ["QUICKSTART_COMMIT"],
    "working_tree_dirty": os.environ["QUICKSTART_DIRTY"] == "true",
    "started_at": datetime.datetime.fromtimestamp(
        started, datetime.timezone.utc
    ).isoformat(),
    "duration_seconds": ended - started,
    "manual_interventions_required": 0 if status == "passed" else None,
    "failed_phase": None if status == "passed" else os.environ["QUICKSTART_FAILED_PHASE"],
    "phases_seconds": phases,
}
path = pathlib.Path(sys.argv[1])
temporary = path.with_suffix(".json.tmp")
temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
temporary.replace(path)
PY
}

finish() {
  local ended_epoch
  ended_epoch="$(date +%s)"
  write_summary "$ended_epoch"
  printf '\nQuickstart %s in %ss (profile: %s)\nKPI: %s\n' \
    "$STATUS" "$((ended_epoch - STARTED_EPOCH))" "$PROFILE_MODE" "$SUMMARY"
}
trap finish EXIT

run_phase() {
  local name="$1"
  shift
  local started="$SECONDS"
  CURRENT_PHASE="$name"
  printf '\n==> %s\n' "$name"
  if "$@"; then
    local duration="$((SECONDS - started))"
    PHASE_RESULTS+=("$name=$duration")
    printf '<== %s complete (%ss)\n' "$name" "$duration"
  else
    local result=$?
    PHASE_RESULTS+=("$name=$((SECONDS - started))")
    printf '<== %s failed; rerun make quickstart after fixing the message above\n' \
      "$name" >&2
    return "$result"
  fi
}

run_phase doctor make -C "$REPO_ROOT" --no-print-directory doctor
run_phase python-environment make -C "$REPO_ROOT" --no-print-directory bootstrap
run_phase local-cluster make -C "$REPO_ROOT" --no-print-directory up-local
run_phase value-proof make -C "$REPO_ROOT" --no-print-directory smoke-local
STATUS=passed
CURRENT_PHASE=complete
