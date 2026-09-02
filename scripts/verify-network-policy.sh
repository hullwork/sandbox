#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/e2e-env.sh
source "${SCRIPT_DIR}/e2e-env.sh"
resolve_e2e_environment
API_URL="${SANDBOX_CONTROL_PLANE_URL}"
SANDBOX_CONTROL_PLANE_IMAGE="${SANDBOX_CONTROL_PLANE_IMAGE:-sandbox-control-plane:0.7.0}"
TASK_TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sandbox-policy.XXXXXX")"
TEST_SUFFIX="$(openssl rand -hex 4)"
POLICY_SESSION_ID="policy-e2e-network-policy"
WORKSPACE_ID=""
SANDBOX_ID=""
PROBE_POD="policy-probe-${TEST_SUFFIX}"
SANDBOX_CONTROL_PLANE_TOKEN=""

KUBECTL_ARGS=(--context "${SANDBOX_KUBE_CONTEXT}")
if [[ -n "${SANDBOX_KUBECONFIG:-}" ]]; then
  KUBECTL_ARGS=(--kubeconfig "${SANDBOX_KUBECONFIG}" --context "${SANDBOX_KUBE_CONTEXT}")
fi

json_get() {
  python3 -c \
    'import json,sys; data=json.load(open(sys.argv[1])); print(data[sys.argv[2]])' \
    "$1" "$2"
}

control_plane_json_post() {
  local url="$1"
  local data="$2"
  local output="$3"
  local attempt
  # A fresh Cilium rollout can report the DaemonSet ready before all service
  # endpoints have converged. Give Control Plane/volume-agent routing a bounded window.
  for ((attempt = 1; attempt <= 30; attempt++)); do
    if curl --fail-with-body --silent --show-error --max-time 45 \
      --request POST \
      --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
      --header "Content-Type: application/json" \
      --data "$data" \
      "$url" >"$output"; then
      return 0
    fi
    sleep 1
  done
  echo "Control Plane request failed after 30 attempts: $url" >&2
  return 1
}

cleanup() {
  set +e
  kubectl "${KUBECTL_ARGS[@]}" --namespace sandbox-system \
    delete pod "${PROBE_POD}" --ignore-not-found --wait=false >/dev/null 2>&1
  if [[ -n "${SANDBOX_ID}" && -n "${SANDBOX_CONTROL_PLANE_TOKEN}" ]]; then
    curl --silent --output /dev/null --max-time 15 \
      --request DELETE \
      --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
      "${API_URL}/v1/sandboxes/${SANDBOX_ID}"
  fi
  rm -rf -- "${TASK_TEMP_DIR}"
}
trap cleanup EXIT

for command in curl kubectl openssl python3; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "missing required command: ${command}" >&2
    exit 1
  fi
done

kubectl "${KUBECTL_ARGS[@]}" --namespace kube-system \
  rollout status daemonset/cilium --timeout=120s
kubectl "${KUBECTL_ARGS[@]}" --namespace kube-system \
  get configmap cilium-config \
  --output=jsonpath='{.data.enable-k8s-networkpolicy}' \
  | grep -qx 'true'

SANDBOX_CONTROL_PLANE_TOKEN="$(
  kubectl "${KUBECTL_ARGS[@]}" --namespace sandbox-system \
    get secret sandbox-api-credentials \
    --output=jsonpath='{.data.control-plane-token}' \
    | python3 -c \
      'import base64,sys; sys.stdout.write(base64.b64decode(sys.stdin.buffer.read()).decode())'
)"

control_plane_json_post \
  "${API_URL}/v1/workspaces" \
  "{\"session_id\":\"${POLICY_SESSION_ID}\"}" \
  "${TASK_TEMP_DIR}/workspace.json"
WORKSPACE_ID="$(json_get "${TASK_TEMP_DIR}/workspace.json" workspace_id)"

control_plane_json_post \
  "${API_URL}/v1/sandboxes" \
  "{\"workspace_id\":\"${WORKSPACE_ID}\"}" \
  "${TASK_TEMP_DIR}/sandbox.json"
SANDBOX_ID="$(json_get "${TASK_TEMP_DIR}/sandbox.json" id)"
SANDBOX_TOKEN="$(json_get "${TASK_TEMP_DIR}/sandbox.json" access_token)"

kubectl "${KUBECTL_ARGS[@]}" --namespace sandbox-workloads \
  wait --for=jsonpath='{.status.state}'=ready \
  "ciliumendpoint/runtime-${SANDBOX_ID}" --timeout=90s
SANDBOX_CONTROL_PLANE_POD_NAME="$(
  kubectl "${KUBECTL_ARGS[@]}" --namespace sandbox-system \
    get pods --selector app.kubernetes.io/name=sandbox-control-plane \
    --output=jsonpath='{.items[0].metadata.name}'
)"
kubectl "${KUBECTL_ARGS[@]}" --namespace sandbox-system \
  wait --for=jsonpath='{.status.state}'=ready \
  "ciliumendpoint/${SANDBOX_CONTROL_PLANE_POD_NAME}" --timeout=90s

SANDBOX_CONTROL_PLANE_CLUSTER_IP="$(
  kubectl "${KUBECTL_ARGS[@]}" --namespace sandbox-system \
    get service sandbox-control-plane --output=jsonpath='{.spec.clusterIP}'
)"
KUBERNETES_CLUSTER_IP="$(
  kubectl "${KUBECTL_ARGS[@]}" --namespace default \
    get service kubernetes --output=jsonpath='{.spec.clusterIP}'
)"
RUNTIME_CLUSTER_IP="$(
  kubectl "${KUBECTL_ARGS[@]}" --namespace sandbox-workloads \
    get service "runtime-${SANDBOX_ID}" --output=jsonpath='{.spec.clusterIP}'
)"

RUNTIME_TEST_COMMAND="$(python3 - "${SANDBOX_CONTROL_PLANE_CLUSTER_IP}" "${KUBERNETES_CLUSTER_IP}" <<'PY'
import sys

control_plane_ip, kubernetes_ip = sys.argv[1:]
print("""python3 - <<'INNER'
import socket

#Semantics (corresponds to sandbox-public-egress NetworkPolicy):
#Private network targets (Control Plane 8080 / kube-apiserver Service 443) must still be rejected;
#Public network TCP 443 (1.1.1.1) and public network DNS resolution (pypi.org) must be reachable.
denied = [(%r, 8080), (%r, 443)]
connected = []
for host, port in denied:
    sock = socket.socket()
    sock.settimeout(2)
    try:
        sock.connect((host, port))
    except OSError:
        pass
    else:
        connected.append(f"{host}:{port}")
    finally:
        sock.close()
if connected:
    print("unexpected egress:", ",".join(connected))
    raise SystemExit(90)

public = socket.socket()
public.settimeout(5)
try:
    public.connect(("1.1.1.1", 443))
except OSError as error:
    print("public egress broken:", error)
    raise SystemExit(91)
finally:
    public.close()

try:
    socket.getaddrinfo("pypi.org", 443)
except OSError as error:
    print("public DNS broken:", error)
    raise SystemExit(92)

print("runtime egress scoped")
INNER""" % (control_plane_ip, kubernetes_ip))
PY
)"

NETWORK_SANDBOX_ID="${SANDBOX_ID}" \
NETWORK_TEST_COMMAND="${RUNTIME_TEST_COMMAND}" \
python3 <<'PY' |
import json
import os

print(json.dumps({
    "jsonrpc": "2.0",
    "id": "network-policy-egress",
    "method": "tools/call",
    "params": {
        "name": "shell",
        "arguments": {
            "action": "exec",
            "command": os.environ["NETWORK_TEST_COMMAND"],
            "timeout_seconds": 10,
        },
    },
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/client": {
            "name": "sandbox-policy-verifier",
            "version": "0.1.0",
        },
    },
}))
PY
  curl --fail-with-body --silent --show-error --max-time 30 \
    --request POST \
    --header "Authorization: Bearer ${SANDBOX_TOKEN}" \
    --header "Content-Type: application/json" \
    --header "Accept: application/json, text/event-stream" \
    --header "MCP-Protocol-Version: 2026-07-28" \
    --header "Mcp-Method: tools/call" \
    --header "Mcp-Name: shell" \
    --data-binary @- \
    "${API_URL}/v1/sandboxes/${SANDBOX_ID}/mcp" \
    >"${TASK_TEMP_DIR}/runtime-egress.json"

python3 - "${TASK_TEMP_DIR}/runtime-egress.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))["result"]["structuredContent"]
assert data["exit_code"] == 0, data
assert "runtime egress scoped" in data["stdout"], data
PY

PROBE_TARGET_IP="${RUNTIME_CLUSTER_IP}" \
PROBE_POD_NAME="${PROBE_POD}" \
PROBE_IMAGE="${SANDBOX_CONTROL_PLANE_IMAGE}" \
python3 <<'PY' | kubectl "${KUBECTL_ARGS[@]}" apply --filename=- >/dev/null
import json
import os

command = """
import socket
import sys

sock = socket.socket()
sock.settimeout(3)
try:
    sock.connect((sys.argv[1], 8080))
except OSError:
    print("non-Control Plane ingress denied")
else:
    print("unexpected non-Control Plane ingress")
    raise SystemExit(90)
finally:
    sock.close()
"""
pod = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {
        "name": os.environ["PROBE_POD_NAME"],
        "namespace": "sandbox-system",
        "labels": {"app.kubernetes.io/name": "network-policy-probe"},
    },
    "spec": {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "runAsGroup": 65532,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [{
            "name": "probe",
            "image": os.environ["PROBE_IMAGE"],
            "imagePullPolicy": "Never",
            "command": ["python3", "-c", command, os.environ["PROBE_TARGET_IP"]],
            "resources": {
                "requests": {"cpu": "5m", "memory": "16Mi"},
                "limits": {"cpu": "50m", "memory": "64Mi"},
            },
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
        }],
    },
}
print(json.dumps(pod))
PY

if ! kubectl "${KUBECTL_ARGS[@]}" --namespace sandbox-system \
  wait --for=jsonpath='{.status.phase}'=Succeeded \
  "pod/${PROBE_POD}" --timeout=45s; then
  kubectl "${KUBECTL_ARGS[@]}" --namespace sandbox-system \
    get pod "${PROBE_POD}" --output=wide >&2 || true
  kubectl "${KUBECTL_ARGS[@]}" --namespace sandbox-system \
    logs "${PROBE_POD}" >&2 || true
  exit 1
fi
kubectl "${KUBECTL_ARGS[@]}" --namespace sandbox-system \
  logs "${PROBE_POD}" | grep -qx 'non-Control Plane ingress denied'

echo "NetworkPolicy verified: Control Plane ingress allowed; Runtime egress and non-Control Plane ingress denied"
