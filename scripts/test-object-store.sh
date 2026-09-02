#!/usr/bin/env bash
#
# End-to-end test of the S3-compatible object API: tickets, import/export,
# versions, scoped credentials, paths, and owner validation.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/e2e-env.sh
source "${SCRIPT_DIR}/e2e-env.sh"
resolve_e2e_environment
API_URL="${SANDBOX_CONTROL_PLANE_URL}"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sandbox-object-store.XXXXXX")"
WORKSPACE_ID=""
SANDBOX_ID=""
SANDBOX_CONTROL_PLANE_TOKEN=""
UPLOAD_ID=""
RUN_ID=""
STAGE="initialize"

cleanup() {
  exit_status=$?
  set +e
  if [[ "$exit_status" -ne 0 ]]; then
    echo "object store E2E failed at stage: $STAGE" >&2
  fi
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
  if [[ -n "${SANDBOX_CONTROL_PLANE_TOKEN}" && -n "${UPLOAD_ID}" ]]; then
    for object_path in source/input.txt source/stream.bin; do
      curl --silent --output /dev/null --request DELETE --get \
        --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
        --data-urlencode "owner=${OBJECT_OWNER:-local/local}" \
        --data-urlencode "scope=upload" \
        --data-urlencode "upload_id=${UPLOAD_ID}" \
        --data-urlencode "path=${object_path}" \
        --data-urlencode "purge_versions=true" \
        "${API_URL}/v1/storage/objects"
    done
  fi
  if [[ -n "${SANDBOX_CONTROL_PLANE_TOKEN}" && -n "${RUN_ID}" ]]; then
    for object_path in artifacts/result.json artifacts/from-workspace.txt; do
      curl --silent --output /dev/null --request DELETE --get \
        --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
        --data-urlencode "owner=${OBJECT_OWNER:-local/local}" \
        --data-urlencode "scope=agent" \
        --data-urlencode "agent_id=test-agent" \
        --data-urlencode "run_id=${RUN_ID}" \
        --data-urlencode "path=${object_path}" \
        --data-urlencode "purge_versions=true" \
        "${API_URL}/v1/storage/objects"
    done
  fi
 # Keep failure evidence: response bodies, including Control Plane errors, live in
 # TEMP_DIR. KEEP_TEMP=1 prevents cleanup after a failed run.
  if [ -n "${KEEP_TEMP:-}" ]; then
    echo "failure scene retained at ${TEMP_DIR}" >&2
  else
    rm -rf "${TEMP_DIR}"
  fi
}
trap cleanup EXIT

decode_base64() {
  python3 -c \
    'import base64,sys; sys.stdout.write(base64.b64decode(sys.stdin.buffer.read()).decode())'
}

assert_json() {
  python3 -c \
    'import json,sys; data=json.load(open(sys.argv[2])); assert eval(sys.argv[1], {"__builtins__": {}}, {"data": data, "len": len}), data' \
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

SANDBOX_CONTROL_PLANE_TOKEN="$(
  kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
    --namespace sandbox-system \
    get secret sandbox-api-credentials \
    --output=jsonpath='{.data.control-plane-token}' \
    | decode_base64
)"
OBJECT_SUFFIX="${OBJECT_SUFFIX:-$(openssl rand -hex 4)}"
OBJECT_OWNER="${OBJECT_OWNER:-local/local}"
UPLOAD_ID="object-store-test-${OBJECT_SUFFIX}"
RUN_ID="run-${OBJECT_SUFFIX}"

STAGE="health"
curl --silent --show-error --fail \
  "${API_URL}/healthz" >"${TEMP_DIR}/health.json"
#Assert "ok" instead of relaxing to "ok"/"unchecked": Control Plane reports truthfully when the probe path is empty
#unchecked, and the object-store-config deployed locally has health-path ——
#If you really receive unchecked, it means that the configuration is drifting. You should fail here instead of letting it go.
assert_json \
  'data["status"] == "ok" and data["object_storage"] == "ok"' \
  "${TEMP_DIR}/health.json"

STAGE="upload-put"
auth_curl \
  --request POST \
  --header "Content-Type: application/json" \
  --data "{\"owner\":\"${OBJECT_OWNER}\",\"scope\":\"upload\",\"upload_id\":\"${UPLOAD_ID}\",\"path\":\"source/input.txt\",\"content_base64\":\"aGVsbG8gdXNlciB1cGxvYWQK\"}" \
  "${API_URL}/v1/storage/objects" >"${TEMP_DIR}/upload-put.json"
assert_json \
  'data["bucket"] == "user-uploads" and data["bytes"] == 18' \
  "${TEMP_DIR}/upload-put.json"

STAGE="upload-get"
auth_curl \
  --get \
  --data-urlencode "owner=${OBJECT_OWNER}" \
  --data-urlencode "scope=upload" \
  --data-urlencode "upload_id=${UPLOAD_ID}" \
  --data-urlencode "path=source/input.txt" \
  "${API_URL}/v1/storage/objects" >"${TEMP_DIR}/upload-get.json"
assert_json \
  'data["content_base64"] == "aGVsbG8gdXNlciB1cGxvYWQK" and data["bytes"] == 18' \
  "${TEMP_DIR}/upload-get.json"

STAGE="upload-list"
auth_curl \
  --get \
  --data-urlencode "owner=${OBJECT_OWNER}" \
  --data-urlencode "scope=upload" \
  --data-urlencode "upload_id=${UPLOAD_ID}" \
  --data-urlencode "path=source" \
  "${API_URL}/v1/storage/objects/list" >"${TEMP_DIR}/upload-list.json"
assert_json \
  'len(data["objects"]) == 1 and data["objects"][0]["bytes"] == 18' \
  "${TEMP_DIR}/upload-list.json"

STAGE="stream-file"
dd if=/dev/zero of="${TEMP_DIR}/stream.bin" bs=1048576 count=5 \
  2>/dev/null
STREAM_BYTES="$(wc -c <"${TEMP_DIR}/stream.bin" | tr -d ' ')"
STREAM_SHA="$(
  openssl dgst -sha256 -r "${TEMP_DIR}/stream.bin" | awk '{print $1}'
)"

STAGE="stream-upload-ticket"
auth_curl \
  --request POST \
  --header "Content-Type: application/json" \
  --data "{\"owner\":\"${OBJECT_OWNER}\",\"operation\":\"upload\",\"scope\":\"upload\",\"upload_id\":\"${UPLOAD_ID}\",\"path\":\"source/stream.bin\",\"content_type\":\"application/octet-stream\",\"max_bytes\":6291456,\"sha256\":\"${STREAM_SHA}\"}" \
  "${API_URL}/v1/storage/tickets" >"${TEMP_DIR}/stream-upload-ticket.json"
assert_json \
  'data["method"] == "PUT" and data["max_bytes"] == 6291456' \
  "${TEMP_DIR}/stream-upload-ticket.json"
UPLOAD_TICKET="$(
  python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["access_token"])' \
    "${TEMP_DIR}/stream-upload-ticket.json"
)"

STAGE="stream-upload"
curl --silent --show-error --fail-with-body \
  --request PUT \
  --header "Authorization: Bearer ${UPLOAD_TICKET}" \
  --header "Content-Type: application/octet-stream" \
  --data-binary "@${TEMP_DIR}/stream.bin" \
  "${API_URL}/v1/storage/content" >"${TEMP_DIR}/stream-upload.json"
python3 -c \
  'import json,sys; data=json.load(open(sys.argv[1])); assert data["bytes"] == int(sys.argv[2]) and data["sha256"] == sys.argv[3], data' \
  "${TEMP_DIR}/stream-upload.json" "${STREAM_BYTES}" "${STREAM_SHA}"

STAGE="ticket-upload-replay"
UPLOAD_REPLAY_STATUS="$(
  curl --silent --output "${TEMP_DIR}/ticket-upload-replay.json" \
    --write-out '%{http_code}' \
    --request PUT \
    --header "Authorization: Bearer ${UPLOAD_TICKET}" \
    --data-binary 'x' \
    "${API_URL}/v1/storage/content"
)"
[[ "${UPLOAD_REPLAY_STATUS}" == "401" ]]

STAGE="ticket-operation-binding"
WRONG_OPERATION_STATUS="$(
  curl --silent --output "${TEMP_DIR}/ticket-wrong-operation.json" \
    --write-out '%{http_code}' \
    --header "Authorization: Bearer ${UPLOAD_TICKET}" \
    "${API_URL}/v1/storage/content"
)"
[[ "${WRONG_OPERATION_STATUS}" == "401" ]]

STAGE="ticket-size-binding"
auth_curl \
  --request POST \
  --header "Content-Type: application/json" \
  --data "{\"owner\":\"${OBJECT_OWNER}\",\"operation\":\"upload\",\"scope\":\"upload\",\"upload_id\":\"${UPLOAD_ID}\",\"path\":\"source/oversize.bin\",\"max_bytes\":1}" \
  "${API_URL}/v1/storage/tickets" >"${TEMP_DIR}/size-ticket.json"
SIZE_TICKET="$(
  python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["access_token"])' \
    "${TEMP_DIR}/size-ticket.json"
)"
SIZE_STATUS="$(
  curl --silent --output "${TEMP_DIR}/ticket-size.json" \
    --write-out '%{http_code}' \
    --request PUT \
    --header "Authorization: Bearer ${SIZE_TICKET}" \
    --data-binary 'xx' \
    "${API_URL}/v1/storage/content"
)"
[[ "${SIZE_STATUS}" == "400" ]]

STAGE="stream-stat"
auth_curl \
  --get \
  --data-urlencode "owner=${OBJECT_OWNER}" \
  --data-urlencode "scope=upload" \
  --data-urlencode "upload_id=${UPLOAD_ID}" \
  --data-urlencode "path=source/stream.bin" \
  "${API_URL}/v1/storage/objects/stat" >"${TEMP_DIR}/stream-stat.json"
python3 -c \
  'import json,sys; data=json.load(open(sys.argv[1])); assert data["bytes"] == int(sys.argv[2]) and data["sha256"] == sys.argv[3] and data["version_id"], data' \
  "${TEMP_DIR}/stream-stat.json" "${STREAM_BYTES}" "${STREAM_SHA}"

STAGE="stream-download-ticket"
auth_curl \
  --request POST \
  --header "Content-Type: application/json" \
  --data "{\"owner\":\"${OBJECT_OWNER}\",\"operation\":\"download\",\"scope\":\"upload\",\"upload_id\":\"${UPLOAD_ID}\",\"path\":\"source/stream.bin\",\"max_bytes\":6291456}" \
  "${API_URL}/v1/storage/tickets" >"${TEMP_DIR}/stream-download-ticket.json"
DOWNLOAD_TICKET="$(
  python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["access_token"])' \
    "${TEMP_DIR}/stream-download-ticket.json"
)"

STAGE="stream-download"
curl --silent --show-error --fail-with-body \
  --header "Authorization: Bearer ${DOWNLOAD_TICKET}" \
  "${API_URL}/v1/storage/content" >"${TEMP_DIR}/stream-download.bin"
cmp "${TEMP_DIR}/stream.bin" "${TEMP_DIR}/stream-download.bin"

STAGE="ticket-download-replay"
DOWNLOAD_REPLAY_STATUS="$(
  curl --silent --output "${TEMP_DIR}/ticket-download-replay.json" \
    --write-out '%{http_code}' \
    --header "Authorization: Bearer ${DOWNLOAD_TICKET}" \
    "${API_URL}/v1/storage/content"
)"
[[ "${DOWNLOAD_REPLAY_STATUS}" == "401" ]]

STAGE="ticket-tamper"
TAMPERED_TICKET="${DOWNLOAD_TICKET%?}A"
if [[ "${TAMPERED_TICKET}" == "${DOWNLOAD_TICKET}" ]]; then
  TAMPERED_TICKET="${DOWNLOAD_TICKET%?}B"
fi
TAMPER_STATUS="$(
  curl --silent --output "${TEMP_DIR}/ticket-tamper.json" \
    --write-out '%{http_code}' \
    --header "Authorization: Bearer ${TAMPERED_TICKET}" \
    "${API_URL}/v1/storage/content"
)"
[[ "${TAMPER_STATUS}" == "401" ]]

STAGE="ticket-expiry"
auth_curl \
  --request POST \
  --header "Content-Type: application/json" \
  --data "{\"owner\":\"${OBJECT_OWNER}\",\"operation\":\"download\",\"scope\":\"upload\",\"upload_id\":\"${UPLOAD_ID}\",\"path\":\"source/stream.bin\",\"max_bytes\":6291456,\"expires_in\":1}" \
  "${API_URL}/v1/storage/tickets" >"${TEMP_DIR}/expired-ticket.json"
EXPIRED_TICKET="$(
  python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["access_token"])' \
    "${TEMP_DIR}/expired-ticket.json"
)"
sleep 2
EXPIRED_STATUS="$(
  curl --silent --output "${TEMP_DIR}/ticket-expired.json" \
    --write-out '%{http_code}' \
    --header "Authorization: Bearer ${EXPIRED_TICKET}" \
    "${API_URL}/v1/storage/content"
)"
[[ "${EXPIRED_STATUS}" == "401" ]]

STAGE="agent-put"
auth_curl \
  --request POST \
  --header "Content-Type: application/json" \
  --data "{\"owner\":\"${OBJECT_OWNER}\",\"scope\":\"agent\",\"agent_id\":\"test-agent\",\"run_id\":\"${RUN_ID}\",\"path\":\"artifacts/result.json\",\"content_base64\":\"e30K\"}" \
  "${API_URL}/v1/storage/objects" >"${TEMP_DIR}/agent-put.json"
assert_json \
  'data["bucket"] == "agent-data" and data["bytes"] == 3' \
  "${TEMP_DIR}/agent-put.json"

STAGE="workspace-create"
auth_curl \
  --request POST \
  --header "Content-Type: application/json" \
  --data "{\"session_id\":\"object-store-bridge-${OBJECT_SUFFIX}\",\"owner\":\"${OBJECT_OWNER}\"}" \
  "${API_URL}/v1/workspaces" >"${TEMP_DIR}/workspace.json"
WORKSPACE_ID="$(
  python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["workspace_id"])' \
    "${TEMP_DIR}/workspace.json"
)"
WORKSPACE_TOKEN="$(
  python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["access_token"])' \
    "${TEMP_DIR}/workspace.json"
)"

#🔴 runtime-create must come before workspace-import. objects/import is to write the content to
#/v1/files/write-binary for Runtime MCP - None
#Runtime has no landing point, Control Plane returns
#   {"error": "object import requires a running Runtime for ws-..."}
#The response body of --fail-with-body will be deleted by trap cleanup, and the troubleshooter will only see
#"curl: (22) ... error: 400". Use KEEP_TEMP=1 to preserve the scene.
STAGE="runtime-create"
auth_curl \
  --request POST \
  --header "Content-Type: application/json" \
  --data "{\"workspace_id\":\"${WORKSPACE_ID}\"}" \
  "${API_URL}/v1/sandboxes" >"${TEMP_DIR}/sandbox.json"
SANDBOX_ID="$(
  python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["id"])' \
    "${TEMP_DIR}/sandbox.json"
)"
SANDBOX_TOKEN="$(
  python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["access_token"])' \
    "${TEMP_DIR}/sandbox.json"
)"
STAGE="workspace-import"
scoped_curl "${WORKSPACE_TOKEN}" \
  --request POST \
  --header "Content-Type: application/json" \
  --data "{\"owner\":\"${OBJECT_OWNER}\",\"scope\":\"upload\",\"upload_id\":\"${UPLOAD_ID}\",\"path\":\"source/input.txt\",\"destination\":\"data/uploads/input.txt\"}" \
  "${API_URL}/v1/workspaces/${WORKSPACE_ID}/objects/import" \
  >"${TEMP_DIR}/workspace-import.json"
assert_json \
  'data["destination"] == "data/uploads/input.txt" and data["file"]["bytes"] == 18' \
  "${TEMP_DIR}/workspace-import.json"

STAGE="runtime-mcp"
#That env | grep is a security assertion: no object storage credentials should be present in the runtime container.
#Check both the old and new sets of variable names - if you only check MINIO, this will become silently invalid after the Control Plane is renamed.
#It becomes an assertion that always passes and guarantees nothing.
python3 -c \
  'import json,sys; payload={"jsonrpc":"2.0","id":"object-store-bridge","method":"tools/call","params":{"name":"shell","arguments":{"action":"exec","command":"cat data/uploads/input.txt; printf agent-result > artifacts/object-store-result.txt; if env | grep -e OBJECT_STORE -e MINIO; then exit 9; fi","timeout_seconds":15}},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/client":{"name":"object-store-e2e","version":"1"}}}; json.dump(payload,open(sys.argv[1],"w"))' \
  "${TEMP_DIR}/mcp.json"
scoped_curl "${SANDBOX_TOKEN}" \
  --request POST \
  --header "Content-Type: application/json" \
  --header "Accept: application/json" \
  --header "MCP-Protocol-Version: 2026-07-28" \
  --header "Mcp-Method: tools/call" \
  --header "Mcp-Name: shell" \
  --data-binary "@${TEMP_DIR}/mcp.json" \
  "${API_URL}/v1/sandboxes/${SANDBOX_ID}/mcp" \
  >"${TEMP_DIR}/mcp-result.json"
python3 -c \
  'import json,sys; data=json.load(open(sys.argv[1]))["result"]["structuredContent"]; assert data["exit_code"] == 0 and data["stdout"] == "hello user upload\n", data' \
  "${TEMP_DIR}/mcp-result.json"

STAGE="workspace-export"
scoped_curl "${WORKSPACE_TOKEN}" \
  --request POST \
  --header "Content-Type: application/json" \
  --data "{\"owner\":\"${OBJECT_OWNER}\",\"scope\":\"agent\",\"agent_id\":\"test-agent\",\"run_id\":\"${RUN_ID}\",\"path\":\"artifacts/from-workspace.txt\",\"workspace_path\":\"artifacts/object-store-result.txt\"}" \
  "${API_URL}/v1/workspaces/${WORKSPACE_ID}/objects/export" \
  >"${TEMP_DIR}/workspace-export.json"
assert_json \
  'data["object"]["bucket"] == "agent-data" and data["object"]["bytes"] == 12' \
  "${TEMP_DIR}/workspace-export.json"

STAGE="agent-get"
auth_curl \
  --get \
  --data-urlencode "owner=${OBJECT_OWNER}" \
  --data-urlencode "scope=agent" \
  --data-urlencode "agent_id=test-agent" \
  --data-urlencode "run_id=${RUN_ID}" \
  --data-urlencode "path=artifacts/from-workspace.txt" \
  "${API_URL}/v1/storage/objects" >"${TEMP_DIR}/agent-get.json"
assert_json \
  'data["content_base64"] == "YWdlbnQtcmVzdWx0" and data["bytes"] == 12' \
  "${TEMP_DIR}/agent-get.json"

STAGE="runtime-release"
auth_curl \
  --request DELETE \
  "${API_URL}/v1/sandboxes/${SANDBOX_ID}" >/dev/null
SANDBOX_ID=""

STAGE="traversal"
TRAVERSAL_STATUS="$(
  curl --silent --output "${TEMP_DIR}/traversal.json" --write-out '%{http_code}' \
    --request POST \
    --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
    --header "Content-Type: application/json" \
    --data "{\"owner\":\"${OBJECT_OWNER}\",\"scope\":\"upload\",\"upload_id\":\"${UPLOAD_ID}\",\"path\":\"../escape\",\"content_base64\":\"eA==\"}" \
    "${API_URL}/v1/storage/objects"
)"
[[ "${TRAVERSAL_STATUS}" == "400" ]]

STAGE="owner-injection"
for BAD_OWNER in "../.." "a/b/c" "" "local"; do
  OWNER_STATUS="$(
    curl --silent --output "${TEMP_DIR}/owner.json" --write-out '%{http_code}' \
      --request POST \
      --header "Authorization: Bearer ${SANDBOX_CONTROL_PLANE_TOKEN}" \
      --header "Content-Type: application/json" \
      --data "{\"owner\":\"${BAD_OWNER}\",\"scope\":\"upload\",\"upload_id\":\"${UPLOAD_ID}\",\"path\":\"source/input.txt\",\"content_base64\":\"eA==\"}" \
      "${API_URL}/v1/storage/objects"
  )"
  [[ "${OWNER_STATUS}" == "400" ]]
done

#It turns out that this paragraph also asserts that the Service type of sandbox-minio is ClusterIP and the PVC is Bound.
#Those two tests are about the storage implementation itself. The object is no longer in the cluster and has gone with the storage unit.
#The remaining difference is that it tests the permission boundaries of the credentials in Control Plane's hand - take Control Plane's own
#Credentials to do admin operations must fail. This is an assertion on the Control Plane side, and it holds true regardless of the storage.
STAGE="credential-scope"
SANDBOX_CONTROL_PLANE_POD="$(
  kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
    --namespace sandbox-system \
    get pod \
    --selector app.kubernetes.io/name=sandbox-control-plane \
    --output=jsonpath='{.items[0].metadata.name}'
)"
# Endpoints and credentials come from Control Plane's own environment so this check
# exercises the exact configured storage target.
kubectl --context "${SANDBOX_KUBE_CONTEXT}" \
  --namespace sandbox-system \
  exec "${SANDBOX_CONTROL_PLANE_POD}" -- sh -ceu '
    raw_endpoint=${OBJECT_STORE_ENDPOINT}
    access_key=${OBJECT_STORE_ACCESS_KEY}
    secret_key=${OBJECT_STORE_SECRET_KEY}
    scheme=${raw_endpoint%%://*}
    host=${raw_endpoint#*://}
    export MC_HOST_sandbox="${scheme}://${access_key}:${secret_key}@${host}"
    export MC_CONFIG_DIR=/tmp/mc
    if mc admin info sandbox >/dev/null 2>&1; then
      exit 1
    fi
  '

STAGE="object-delete"
auth_curl \
  --request DELETE \
  --get \
  --data-urlencode "owner=${OBJECT_OWNER}" \
  --data-urlencode "scope=upload" \
  --data-urlencode "upload_id=${UPLOAD_ID}" \
  --data-urlencode "path=source/stream.bin" \
  "${API_URL}/v1/storage/objects" >"${TEMP_DIR}/object-delete.json"
assert_json \
  'data["deleted"] and data["history_retained"]' \
  "${TEMP_DIR}/object-delete.json"

STAGE="object-versions"
auth_curl \
  --get \
  --data-urlencode "owner=${OBJECT_OWNER}" \
  --data-urlencode "scope=upload" \
  --data-urlencode "upload_id=${UPLOAD_ID}" \
  --data-urlencode "path=source/stream.bin" \
  "${API_URL}/v1/storage/objects/versions" \
  >"${TEMP_DIR}/object-versions.json"
python3 -c \
  'import json,sys; data=json.load(open(sys.argv[1])); versions=data["versions"]; assert len(versions) >= 2 and any(v["delete_marker"] and v["is_latest"] for v in versions) and any(not v["delete_marker"] and v["bytes"] == 5242880 for v in versions), data' \
  "${TEMP_DIR}/object-versions.json"

#MINIO_RESTART_TEST=1 section (delete sandbox-minio-0, wait for StatefulSet to come back, and then verify the object
#Still readable) deleted: The storage unit is not in this cluster. It cannot be restarted here and it is not appropriate to restart other people's storage.
#Handed over to the storage unit: "The object is still there after restarting" is its persistence self-test.
#The remaining equivalent gap in this cluster is "the control plane can self-heal after the storage jitter is restored", which must be done in the storage unit
#It was only measured in a controlled environment, but it is not covered now.

STAGE="complete"
echo "PASS: object APIs, upload→Runtime import, Agent export, version semantics, and limited credentials"
