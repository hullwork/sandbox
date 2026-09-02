#!/usr/bin/env bash
# Unified entry point for sandbox cluster-level E2E tests.
#
# It runs the five cluster-level scripts in dependency order and emits a
# machine-readable summary. It does not create a cluster or open port forwards;
# run scripts/local-cluster.sh up first. New cluster-level scripts must be added
# here or they are effectively unreachable from CI and release validation.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/e2e-env.sh
source "${SCRIPT_DIR}/e2e-env.sh"
resolve_e2e_environment || exit $?
API_URL="${SANDBOX_CONTROL_PLANE_URL}"
PYTHON="${PYTHON:-python3}"

ONLY="${1:-all}"

#The order is meaningful: a priori network strategy (cheapest, failure means the cluster is not configured properly),
#Then run the functional side, and finally run restart and confrontation (the slowest and will restart the Runtime).
SCRIPTS=(
  "verify-network-policy.sh"
  "test.sh"
  "test-object-store.sh"
  "test-restart.sh"
  "test-adversarial.py"
)

#Detect these two namespaces instead of other names: these are the five scripts that are really useful
#(sandbox-system is for Control Plane, and sandbox-workloads is for Runtime/Workspace).
for ns in sandbox-system sandbox-workloads; do
  if ! kubectl --context "${SANDBOX_KUBE_CONTEXT}" get ns "${ns}" >/dev/null 2>&1; then
    echo "FATAL: cluster ${SANDBOX_KUBE_CONTEXT} is unreachable or namespace ${ns} is missing; run scripts/local-cluster.sh up first" >&2
    printf '{"status":"error","reason":"cluster-unreachable","cluster":"%s","missing_namespace":"%s"}\n' \
      "${SANDBOX_KUBE_CONTEXT}" "${ns}"
    exit 2
  fi
done
if ! curl -sf -o /dev/null --max-time 10 "${API_URL}/healthz"; then
  echo "FATAL: Control Plane ${API_URL} is unreachable; port-forward sandbox-control-plane first" >&2
  echo '{"status":"error","reason":"control-plane-unreachable","url":"'"${API_URL}"'"}'
  exit 2
fi

pass=0; fail=0; ran=0
declare -a RESULTS=()
started_all=$(date +%s)

for s in "${SCRIPTS[@]}"; do
  [ "${ONLY}" != "all" ] && [ "${ONLY}" != "${s}" ] && continue
  path="${SCRIPT_DIR}/${s}"
  if [ ! -f "${path}" ]; then
    echo "!! missing: ${s}" >&2; fail=$((fail+1))
    RESULTS+=("{\"script\":\"${s}\",\"result\":\"missing\"}")
    continue
  fi
  echo "=== ${s} ==="
  started=$(date +%s)
  if [ "${s##*.}" = "py" ]; then "${PYTHON}" "${path}"; else bash "${path}"; fi
  rc=$?
  elapsed=$(( $(date +%s) - started ))
  ran=$((ran+1))
  if [ ${rc} -eq 0 ]; then
    pass=$((pass+1)); echo "--- ${s} PASS (${elapsed}s)"
    RESULTS+=("{\"script\":\"${s}\",\"result\":\"pass\",\"seconds\":${elapsed}}")
  else
    fail=$((fail+1)); echo "--- ${s} FAIL rc=${rc} (${elapsed}s)"
    RESULTS+=("{\"script\":\"${s}\",\"result\":\"fail\",\"rc\":${rc},\"seconds\":${elapsed}}")
  fi
done

total_elapsed=$(( $(date +%s) - started_all ))
echo
echo "=== summary: ran ${ran}, passed ${pass}, failed ${fail}, elapsed ${total_elapsed}s ==="
printf '{"status":"%s","ran":%d,"passed":%d,"failed":%d,"seconds":%d,"cluster":"%s","scripts":[%s]}\n' \
  "$([ ${fail} -eq 0 ] && echo passed || echo failed)" \
  "${ran}" "${pass}" "${fail}" "${total_elapsed}" "${SANDBOX_KUBE_CONTEXT}" \
  "$(IFS=,; echo "${RESULTS[*]}")"

#Running 0 times is also a failure: the meaning of this entry is "someone did run", and skipping silently is equivalent to returning to the state before the problem occurred.
[ ${ran} -eq 0 ] && { echo "FATAL: no tests ran (ONLY=${ONLY} matched no scripts)" >&2; exit 2; }
[ ${fail} -eq 0 ]
