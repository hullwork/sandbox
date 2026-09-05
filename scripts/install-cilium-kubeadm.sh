#!/usr/bin/env bash
# Install the checksum-pinned Cilium chart in the local kubeadm cluster.
set -euo pipefail

CLUSTER="${1:?usage: install-cilium-kubeadm.sh <cluster>}"
CILIUM_VERSION="${CILIUM_VERSION:-1.19.6}"
CILIUM_CHART_SHA256="${CILIUM_CHART_SHA256:-21c43cf53841f9ab0375047d95aa4c64051ea52bbd2c679416e6408f5f1c9179}"

: "${KUBECONFIG:?set KUBECONFIG to the target kubeadm cluster configuration}"
# The default-value branch creates an ordinary shell variable that Helm cannot
# see in its child process; export KUBECONFIG explicitly.
export KUBECONFIG

TASK_TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sandbox-cilium-kubeadm.XXXXXX")"
cleanup() {
  rm -rf -- "${TASK_TEMP_DIR}"
}
trap cleanup EXIT

for command in helm shasum; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "missing required command: ${command}" >&2
    exit 1
  fi
done

#Convention: The caller passes a single kubeconfig path (true for every entry point that calls this script).
if [ ! -s "${KUBECONFIG%%:*}" ]; then
  echo "kubeconfig not found or empty: ${KUBECONFIG}" >&2
  exit 1
fi

CHART_FILE="${TASK_TEMP_DIR}/cilium-${CILIUM_VERSION}.tgz"
helm pull oci://quay.io/cilium/charts/cilium \
  --version "${CILIUM_VERSION}" \
  --destination "${TASK_TEMP_DIR}"
ACTUAL_CHART_SHA256="$(shasum -a 256 "${CHART_FILE}" | awk '{print $1}')"
if [[ "${ACTUAL_CHART_SHA256}" != "${CILIUM_CHART_SHA256}" ]]; then
  echo "Cilium chart checksum mismatch: expected ${CILIUM_CHART_SHA256}, got ${ACTUAL_CHART_SHA256}" >&2
  exit 1
fi

# One operator replica keeps the local profile lightweight as workers scale up
# and down; the Cilium agent itself remains a DaemonSet on every active node.
# helm --wait; --set-string prevents Helm from parsing false as a boolean.
helm --kube-context "${CLUSTER}" upgrade --install cilium "${CHART_FILE}" \
  --namespace kube-system \
  --set routingMode=tunnel --set tunnelProtocol=vxlan \
  --set ipam.mode=kubernetes \
  --set-string kubeProxyReplacement=false \
  --set operator.replicas=1 \
  --wait --timeout 10m

echo "Cilium ${CILIUM_VERSION} (tunnel/vxlan) installed on ${CLUSTER}; chart sha256: ${ACTUAL_CHART_SHA256}"
