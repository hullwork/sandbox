#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/e2e-env.sh
source "${SCRIPT_DIR}/e2e-env.sh"
resolve_e2e_environment
API_URL="${SANDBOX_CONTROL_PLANE_URL}"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sandbox-restart.XXXXXX")"
WORKSPACE_ID=""
SANDBOX_ID=""

cleanup() {
  set +e
  if [[ -n "${SANDBOX_ID}" ]]; then
    curl --silent --output /dev/null \
      --request DELETE \
      --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN:-}" \
      "${API_URL}/v1/sandboxes/${SANDBOX_ID}"
  fi
  if [[ -n "${WORKSPACE_ID}" ]]; then
    curl --silent --output /dev/null \
      --request DELETE \
      --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN:-}" \
      "${API_URL}/v1/workspaces/${WORKSPACE_ID}?purge=true"
  fi
  rm -rf "${TEMP_DIR}"
}
trap cleanup EXIT

decode_base64() {
  python3 -c \
    'import base64,sys; sys.stdout.write(base64.b64decode(sys.stdin.buffer.read()).decode())'
}

json_field() {
  python3 -c \
    'import json,sys; print(json.load(open(sys.argv[2]))[sys.argv[1]])' \
    "$1" "$2"
}

auth_curl() {
  curl --silent --show-error --fail-with-body \
    --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
    "$@"
}

scoped_curl() {
  local token="$1"
  shift
  curl --silent --show-error --fail-with-body \
    --header "Authorization: Bearer ${token}" \
    "$@"
}

runtime_exec() {
  local command="$1"
  local output_file="$2"
  local attempt
  python3 -c \
    'import json,sys; json.dump({"jsonrpc":"2.0","id":"restart-e2e","method":"tools/call","params":{"name":"shell","arguments":{"action":"exec","command":sys.argv[2],"timeout_seconds":15}},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/client":{"name":"restart-e2e","version":"1"}}},open(sys.argv[1],"w"))' \
    "${TEMP_DIR}/mcp.json" "${command}"
  for ((attempt = 1; attempt <= 15; attempt++)); do
    if scoped_curl "${SANDBOX_TOKEN}" \
      --request POST \
      --header "Content-Type: application/json" \
      --header "Accept: application/json" \
      --header "MCP-Protocol-Version: 2026-07-28" \
      --header "Mcp-Method: tools/call" \
      --header "Mcp-Name: shell" \
      --data-binary "@${TEMP_DIR}/mcp.json" \
      "${API_URL}/v1/sandboxes/${SANDBOX_ID}/mcp" >"${output_file}"; then
      return 0
    fi
    sleep 1
  done
  echo "Runtime MCP request failed after restart convergence window" >&2
  return 1
}

SANDBOX_CONTROL_PLANE_TOKEN="$(
  kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
    --namespace sandbox-system \
    get secret sandbox-api-credentials \
    --output=jsonpath='{.data.control-plane-token}' \
    | decode_base64
)"
SESSION_SUFFIX="$(openssl rand -hex 6)"

auth_curl \
  --request POST \
  --header "Content-Type: application/json" \
  --data "{\"session_id\":\"restart-${SESSION_SUFFIX}\"}" \
  "${API_URL}/v1/workspaces" >"${TEMP_DIR}/workspace.json"
WORKSPACE_ID="$(json_field workspace_id "${TEMP_DIR}/workspace.json")"
WORKSPACE_TOKEN="$(json_field access_token "${TEMP_DIR}/workspace.json")"

auth_curl \
  --request POST \
  --header "Content-Type: application/json" \
  --data "{\"workspace_id\":\"${WORKSPACE_ID}\"}" \
  "${API_URL}/v1/sandboxes" >"${TEMP_DIR}/sandbox.json"
SANDBOX_ID="$(json_field id "${TEMP_DIR}/sandbox.json")"
SANDBOX_TOKEN="$(json_field access_token "${TEMP_DIR}/sandbox.json")"

scoped_curl "${WORKSPACE_TOKEN}" \
  --request POST \
  --header "Content-Type: application/json" \
  --data '{"path":"restart.txt","content":"survived runtime restart\n"}' \
  "${API_URL}/v1/workspaces/${WORKSPACE_ID}/files/write" >/dev/null

runtime_exec \
  'printf runtime-before > artifacts/runtime-marker.txt; cat restart.txt' \
  "${TEMP_DIR}/before.json"
python3 -c \
  'import json,sys; data=json.load(open(sys.argv[1]))["result"]["structuredContent"]; assert data["exit_code"] == 0 and data["stdout"] == "survived runtime restart\n", data' \
  "${TEMP_DIR}/before.json"

RUNTIME_POD="$(
  kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
    --namespace sandbox-workloads \
    get pod \
    --selector "convee.io/sandbox-id=${SANDBOX_ID}" \
    --output=jsonpath='{.items[0].metadata.name}'
)"
RESTART_BEFORE="$(
  kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
    --namespace sandbox-workloads \
    get pod "${RUNTIME_POD}" \
    --output=jsonpath='{.status.containerStatuses[?(@.name=="shell-mcp")].restartCount}'
)"

# The Runtime server is PID 1 and runs as the unprivileged sandbox user. Its
# abrupt exit exercises the Pod restart path without deleting the Workspace.
#Explicitly specify -c shell-mcp to avoid kubectl selecting the wrong container after adding a sidecar to the Runtime Pod in the future;
#Killing the wrong container will cause shell-mcp's restartCount to never move and wait until timeout, while the following
#2>/dev/null will also swallow exec’s own error.
if ! kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
  --namespace sandbox-workloads \
  exec "${RUNTIME_POD}" -c shell-mcp -- kill -KILL 1 >/dev/null 2>&1; then
 # kill -KILL 1 interrupts this exec's own connection, and a non-zero exit is expected;
 # But real failure (container does not exist/Pod does not exist) also occurs here, so the following is determined by restartCount.
  :
fi

deadline=$((SECONDS + 120))
while [[ "${SECONDS}" -lt "${deadline}" ]]; do
  restart_now="$(
    kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
      --namespace sandbox-workloads \
      get pod "${RUNTIME_POD}" \
      --output=jsonpath='{.status.containerStatuses[?(@.name=="shell-mcp")].restartCount}' \
      2>/dev/null || true
  )"
  ready_now="$(
    kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
      --namespace sandbox-workloads \
      get pod "${RUNTIME_POD}" \
      --output=jsonpath='{.status.containerStatuses[?(@.name=="shell-mcp")].ready}' \
      2>/dev/null || true
  )"
  if [[ "${restart_now:-0}" -gt "${RESTART_BEFORE}" && "${ready_now}" == "true" ]]; then
    break
  fi
  sleep 1
done

RESTART_AFTER="$(
  kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
    --namespace sandbox-workloads \
    get pod "${RUNTIME_POD}" \
    --output=jsonpath='{.status.containerStatuses[?(@.name=="shell-mcp")].restartCount}'
)"
READY_AFTER="$(
  kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
    --namespace sandbox-workloads \
    get pod "${RUNTIME_POD}" \
    --output=jsonpath='{.status.containerStatuses[?(@.name=="shell-mcp")].ready}'
)"
[[ "${RESTART_AFTER}" -gt "${RESTART_BEFORE}" && "${READY_AFTER}" == "true" ]]

runtime_exec \
  'cat artifacts/runtime-marker.txt; echo; cat restart.txt' \
  "${TEMP_DIR}/after.json"
python3 -c \
  'import json,sys; data=json.load(open(sys.argv[1]))["result"]["structuredContent"]; assert data["exit_code"] == 0 and data["stdout"].splitlines() == ["runtime-before", "survived runtime restart"], data' \
  "${TEMP_DIR}/after.json"

scoped_curl "${WORKSPACE_TOKEN}" \
  --get \
  --data-urlencode "path=restart.txt" \
  "${API_URL}/v1/workspaces/${WORKSPACE_ID}/files/read" \
  >"${TEMP_DIR}/workspace-after.json"
python3 -c \
  'import json,sys; data=json.load(open(sys.argv[1])); assert data["content"] == "survived runtime restart\n", data' \
  "${TEMP_DIR}/workspace-after.json"

auth_curl \
  --request DELETE \
  "${API_URL}/v1/sandboxes/${SANDBOX_ID}" >/dev/null
SANDBOX_ID=""
auth_curl \
  --request DELETE \
  "${API_URL}/v1/workspaces/${WORKSPACE_ID}?purge=true" >/dev/null
WORKSPACE_ID=""

echo "PASS: Runtime PID 1 restarted and original Sandbox token and Workspace files recovered"
