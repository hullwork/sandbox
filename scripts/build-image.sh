#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 4 ] || [ "$2" != -f ]; then
  echo "usage: scripts/build-image.sh <tag> -f <dockerfile> <context>" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGE="$1"
DOCKERFILE="$3"
CONTEXT="$4"
LOG_DIR="${SANDBOX_BUILD_LOG_DIR:-$REPO_ROOT/.sandbox/logs/images}"
PROGRESS="${DOCKER_BUILD_PROGRESS:-plain}"
LOG_FILE="$LOG_DIR/${IMAGE//[:\/]/-}.log"
STARTED_AT="$SECONDS"

mkdir -p "$LOG_DIR"
printf '  building %-32s (log: %s)\n' "$IMAGE" "$LOG_FILE"
if docker build --progress="$PROGRESS" -t "$IMAGE" -f "$DOCKERFILE" "$CONTEXT" >"$LOG_FILE" 2>&1; then
  printf '  built    %-32s %ss\n' "$IMAGE" "$((SECONDS - STARTED_AT))"
else
  status=$?
  printf '  failed   %-32s %ss; last 80 log lines follow\n' \
    "$IMAGE" "$((SECONDS - STARTED_AT))" >&2
  tail -n 80 "$LOG_FILE" >&2
  exit "$status"
fi
