#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/e2e-env.sh
source "${SCRIPT_DIR}/e2e-env.sh"
resolve_e2e_environment
API_URL="${SANDBOX_CONTROL_PLANE_URL}"
#Must be consistent with Control Plane's SANDBOX_RUNTIME_CLASS. Use ${VAR-default} instead
#${VAR:-default}: The explicitly exported empty string is the legal configuration of "no runtimeClass".
#:- will mistake it for not being set and quietly switch back to gvisor, asserting that the path will no longer be detected.
SANDBOX_RUNTIME_CLASS="${SANDBOX_RUNTIME_CLASS-gvisor}"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sandbox-e2e.XXXXXX")"
SANDBOX_ID=""
SANDBOX_ID_2=""
WORKSPACE_ID=""
WORKSPACE_ID_2=""
SDK_WORKSPACE_ID=""
TEST_SUFFIX="$(openssl rand -hex 4)"

json_get() {
  python3 -c \
    'import json,sys; data=json.load(open(sys.argv[1])); print(data[sys.argv[2]])' \
    "$1" "$2"
}

cleanup() {
  set +e
  for sandbox_id in "$SANDBOX_ID" "$SANDBOX_ID_2"; do
    if [ -n "$sandbox_id" ]; then
      curl --silent --output /dev/null \
        --request DELETE \
        --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
        "${API_URL}/v1/sandboxes/${sandbox_id}"
    fi
  done
  for workspace_id in "$WORKSPACE_ID" "$WORKSPACE_ID_2" "$SDK_WORKSPACE_ID"; do
    if [ -n "$workspace_id" ]; then
      curl --silent --output /dev/null \
        --request DELETE \
        --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
        "${API_URL}/v1/workspaces/${workspace_id}?purge=true"
    fi
  done
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

SANDBOX_CONTROL_PLANE_TOKEN="$(
  kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
    --namespace sandbox-system \
    get secret sandbox-api-credentials \
    --output=jsonpath='{.data.control-plane-token}' \
    | python3 -c \
      'import base64,sys; sys.stdout.write(base64.b64decode(sys.stdin.buffer.read()).decode())'
)"

curl --fail --silent --show-error \
  "${API_URL}/healthz" >"${TEMP_DIR}/health.json"
python3 -c \
  'import json,sys; data=json.load(open(sys.argv[1])); assert data.get("status") == "ok" and data.get("kubernetes") == "ok", data' \
  "${TEMP_DIR}/health.json"

curl --fail-with-body --silent --show-error \
  --request POST \
  --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
  --header "Content-Type: application/json" \
  --data "{\"session_id\":\"e2e-session-one-${TEST_SUFFIX}\"}" \
  "${API_URL}/v1/workspaces" >"${TEMP_DIR}/workspace.json"
WORKSPACE_ID="$(json_get "${TEMP_DIR}/workspace.json" workspace_id)"
WORKSPACE_TOKEN="$(json_get "${TEMP_DIR}/workspace.json" access_token)"

curl --fail-with-body --silent --show-error \
  --request POST \
  --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
  --header "Content-Type: application/json" \
  --data "{\"workspace_id\":\"${WORKSPACE_ID}\"}" \
  "${API_URL}/v1/sandboxes" >"${TEMP_DIR}/sandbox.json"
SANDBOX_ID="$(json_get "${TEMP_DIR}/sandbox.json" id)"
SANDBOX_TOKEN="$(json_get "${TEMP_DIR}/sandbox.json" access_token)"

curl --fail-with-body --silent --show-error \
  --request POST \
  --header "Authorization: Bearer ${WORKSPACE_TOKEN}" \
  --header "Content-Type: application/json" \
  --data '{"path":"shared.txt","content":"from-file\n"}' \
  "${API_URL}/v1/workspaces/${WORKSPACE_ID}/files/write" \
  >"${TEMP_DIR}/write.json"

python3 - "$SANDBOX_ID" <<'PY' |
import json
import sys

sandbox_id = sys.argv[1]
command = """
set -u
printf 'from-runtime\\n' >> shared.txt
cat shared.txt
id -u
test ! -e /var/run/secrets/kubernetes.io/serviceaccount/token
if touch /etc/runtime-rootfs-must-be-read-only 2>/dev/null; then exit 90; fi
dmesg | sed -n '1p'
"""
print(json.dumps({
    "jsonrpc": "2.0",
    "id": "e2e-shell-1",
    "method": "tools/call",
    "params": {
        "name": "shell",
        "arguments": {
            "action": "exec",
            "command": command,
            "timeout_seconds": 15,
        },
    },
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/client": {
            "name": "sandbox-e2e",
            "version": "0.2.0",
        },
    },
}))
PY
  curl --fail-with-body --silent --show-error \
    --request POST \
    --header "Authorization: Bearer ${SANDBOX_TOKEN}" \
    --header "Content-Type: application/json" \
    --header "Accept: application/json, text/event-stream" \
    --header "MCP-Protocol-Version: 2026-07-28" \
    --header "Mcp-Method: tools/call" \
    --header "Mcp-Name: shell" \
    --data-binary @- \
    "${API_URL}/v1/sandboxes/${SANDBOX_ID}/mcp" \
    >"${TEMP_DIR}/shell.json"

#"gVisor" in the first line of dmesg is self-reported by runsc and is only available when running gvisor runtimeClass;
#When returning to the default runtime of the cluster, that line is the startup log of the host kernel, and it must be a false alarm to assert it.
python3 -c \
  'import json,sys; data=json.load(open(sys.argv[1]))["result"]["structuredContent"]; assert data["exit_code"] == 0, data; assert data["stdout"].splitlines()[:3] == ["from-file","from-runtime","65532"], data; assert sys.argv[2] != "gvisor" or "gVisor" in data["stdout"], data' \
  "${TEMP_DIR}/shell.json" "${SANDBOX_RUNTIME_CLASS}"

curl --fail-with-body --silent --show-error \
  --header "Authorization: Bearer ${WORKSPACE_TOKEN}" \
  "${API_URL}/v1/workspaces/${WORKSPACE_ID}/files/read?path=shared.txt" \
  >"${TEMP_DIR}/shared-after-runtime.json"
python3 -c \
  'import json,sys; assert json.load(open(sys.argv[1]))["content"] == "from-file\nfrom-runtime\n"' \
  "${TEMP_DIR}/shared-after-runtime.json"

python3 - "$SANDBOX_ID" <<'PY' |
import json
import sys

print(json.dumps({
    "jsonrpc": "2.0",
    "id": "e2e-network",
    "method": "tools/call",
    "params": {
        "name": "shell",
        "arguments": {
            "action": "exec",
            "command": "wget -T 3 -qO- https://example.com >/dev/null 2>&1; code=$?; printf '%s\\n' \"$code\"",
            "timeout_seconds": 8,
        },
    },
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/client": {"name": "sandbox-e2e", "version": "0.2.0"},
    },
}))
PY
  curl --fail-with-body --silent --show-error \
    --request POST \
    --header "Authorization: Bearer ${SANDBOX_TOKEN}" \
    --header "Content-Type: application/json" \
    --header "Accept: application/json, text/event-stream" \
    --header "MCP-Protocol-Version: 2026-07-28" \
    --header "Mcp-Method: tools/call" \
    --header "Mcp-Name: shell" \
    --data-binary @- \
    "${API_URL}/v1/sandboxes/${SANDBOX_ID}/mcp" \
    >"${TEMP_DIR}/network.json"
#a7d90a8 After directional release of public network egress (kube-dns 53 + non-private network TCP 80/443), wget should
#Success (exit 0). The old assertion `not in {"", "0"}` was written in the default-deny era - expectation
#Rejected from the network; after being released, it relied on the host fake-ip for a long time. DNS resolution failed "coincidentally" green, 2026-08-20 cluster
#After the merger, DNS is normal and wget is truly connected. The private network is still asserted by the positive control of default-deny
#in verify-network-policy.sh, not repeated here.
python3 -c \
  'import json,sys; data=json.load(open(sys.argv[1]))["result"]["structuredContent"]; assert data["exit_code"] == 0 and data["stdout"].strip() == "0", data' \
  "${TEMP_DIR}/network.json"

runtime_class="$(
  kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
    --namespace sandbox-workloads \
    get pod "runtime-${SANDBOX_ID}" \
    --output=jsonpath='{.spec.runtimeClassName}'
)"
runtime_subpath="$(
  kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
    --namespace sandbox-workloads \
    get pod "runtime-${SANDBOX_ID}" \
    --output=jsonpath='{.spec.containers[0].volumeMounts[?(@.mountPath=="/workspace")].subPath}'
)"
workspace_storage_mode="$(
  kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
    --namespace sandbox-system \
    get deployment sandbox-control-plane \
    --output=jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="SANDBOX_WORKSPACE_STORAGE_MODE")].value}'
)"
# Runtime MCP is the only application container; workspace files are not
# served by a sidecar.  The per-workspace PVC is mounted at its root.
runtime_containers="$(
  kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
    --namespace sandbox-workloads \
    get pod "runtime-${SANDBOX_ID}" \
    --output=jsonpath='{.spec.containers[*].name}'
)"
#When empty, jsonpath cannot obtain the field and returns an empty string, which is exactly equal to the expected value of empty - at the same time
#It proves that there is indeed no runtimeClassName key in the Pod spec.
test "$runtime_class" = "${SANDBOX_RUNTIME_CLASS}"
case "$workspace_storage_mode" in
  shared) test "$runtime_subpath" = "$WORKSPACE_ID" ;;
  per-workspace) test -z "$runtime_subpath" ;;
  *) echo "unsupported workspace storage mode: $workspace_storage_mode" >&2; exit 1 ;;
esac
test "$runtime_containers" = "shell-mcp"

curl --fail-with-body --silent --show-error \
  --request DELETE \
  --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
  "${API_URL}/v1/sandboxes/${SANDBOX_ID}" >"${TEMP_DIR}/release.json"
SANDBOX_ID=""

# File APIs require a mounted Runtime. Recreate it for the same workspace to
# prove that the PVC survived release rather than relying on a host-side fallback.
curl --fail-with-body --silent --show-error \
  --request POST \
  --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
  --header "Content-Type: application/json" \
  --data "{\"workspace_id\":\"${WORKSPACE_ID}\"}" \
  "${API_URL}/v1/sandboxes" >"${TEMP_DIR}/sandbox-after-release.json"
SANDBOX_ID="$(json_get "${TEMP_DIR}/sandbox-after-release.json" id)"
SANDBOX_TOKEN="$(json_get "${TEMP_DIR}/sandbox-after-release.json" access_token)"

curl --fail-with-body --silent --show-error \
  --header "Authorization: Bearer ${WORKSPACE_TOKEN}" \
  "${API_URL}/v1/workspaces/${WORKSPACE_ID}/files/read?path=shared.txt" \
  >"${TEMP_DIR}/shared-after-release.json"
python3 -c \
  'import json,sys; assert json.load(open(sys.argv[1]))["content"] == "from-file\nfrom-runtime\n"' \
  "${TEMP_DIR}/shared-after-release.json"

curl --fail-with-body --silent --show-error \
  --request POST \
  --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
  --header "Content-Type: application/json" \
  --data "{\"session_id\":\"e2e-session-two-${TEST_SUFFIX}\"}" \
  "${API_URL}/v1/workspaces" >"${TEMP_DIR}/workspace-two.json"
WORKSPACE_ID_2="$(json_get "${TEMP_DIR}/workspace-two.json" workspace_id)"
WORKSPACE_TOKEN_2="$(json_get "${TEMP_DIR}/workspace-two.json" access_token)"
test "$WORKSPACE_ID" != "$WORKSPACE_ID_2"

cross_status="$(
  curl --silent --output "${TEMP_DIR}/cross-token.json" \
    --write-out '%{http_code}' \
    --header "Authorization: Bearer ${WORKSPACE_TOKEN}" \
    "${API_URL}/v1/workspaces/${WORKSPACE_ID_2}/files/list?path="
)"
test "$cross_status" = "401"

sdk_output="$(
  cd "$REPO_DIR"
  SANDBOX_CONTROL_PLANE_URL="$API_URL" \
  SANDBOX_TOKEN="$SANDBOX_CONTROL_PLANE_TOKEN" \
  SANDBOX_E2E_SESSION="sandbox-e2e-${TEST_SUFFIX}" \
  python3 - <<'PY'
import json
import os
from sandbox_platform import sandbox_client

with sandbox_client.session_context(os.environ["SANDBOX_E2E_SESSION"]):
    manager = sandbox_client.SandboxManager()
    wrote = manager.write_file("sdk.txt", "from-sdk\n")
    assert wrote.get("path") == "sdk.txt" and wrote.get("bytes") == 9, wrote
    shell = manager.shell("printf 'from-shell\\n' >> sdk.txt")
    assert shell.get("exit_code") == 0, shell
    read = manager.read_file("sdk.txt")
    assert read.get("content") == "from-sdk\nfrom-shell\n", read
    status = manager.status()
    print(json.dumps({
        "workspace_id": status["workspace_id"],
        "sandbox_id": status["sandbox_id"],
    }))
PY
)"
SDK_WORKSPACE_ID="$(
  printf '%s' "$sdk_output" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["workspace_id"])'
)"

# A fresh process must resolve the name without enumerating or recreating the
# Workspace, then release the active Runtime while preserving its files.
(
  cd "$REPO_DIR"
  SANDBOX_CONTROL_PLANE_URL="$API_URL" \
  SANDBOX_TOKEN="$SANDBOX_CONTROL_PLANE_TOKEN" \
  SANDBOX_E2E_SESSION="sandbox-e2e-${TEST_SUFFIX}" \
  python3 - <<'PY'
import os
from sandbox_platform import sandbox_client

sandbox = sandbox_client.Sandbox.get(os.environ["SANDBOX_E2E_SESSION"])
released = sandbox.stop()
assert released.get("released") is True, released
PY
)

fallback_dir="${TEMP_DIR}/fallback"
mkdir -p "$fallback_dir"
(
  cd "$fallback_dir"
  PYTHONPATH="$REPO_DIR" \
  SANDBOX_CONTROL_PLANE_URL="http://127.0.0.1:1" \
  SANDBOX_TOKEN="unreachable-test-token" \
  python3 - <<'PY'
import pathlib
from sandbox_platform import sandbox_client

with sandbox_client.session_context("control-plane-unavailable-e2e"):
    try:
        sandbox_client.SandboxManager().shell("touch host-fallback-marker")
    except sandbox_client.ControlPlaneError:
        pass
    else:
        raise AssertionError("unreachable Control Plane unexpectedly executed shell")
assert not pathlib.Path("host-fallback-marker").exists()
PY
)

curl --fail-with-body --silent --show-error \
  --request DELETE \
  --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
  "${API_URL}/v1/sandboxes/${SANDBOX_ID}" >/dev/null
SANDBOX_ID=""

curl --fail-with-body --silent --show-error \
  --request DELETE \
  --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
  "${API_URL}/v1/workspaces/${WORKSPACE_ID}?purge=true" >/dev/null
WORKSPACE_ID=""
curl --fail-with-body --silent --show-error \
  --request DELETE \
  --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
  "${API_URL}/v1/workspaces/${WORKSPACE_ID_2}?purge=true" >/dev/null
WORKSPACE_ID_2=""
curl --fail-with-body --silent --show-error \
  --request DELETE \
  --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
  "${API_URL}/v1/workspaces/${SDK_WORKSPACE_ID}?purge=true" >/dev/null
SDK_WORKSPACE_ID=""

echo "Sandbox E2E passed: Runtime MCP files/shell + shared Workspace + gVisor"
