#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VM_NAME="${SANDBOX_LOCAL_VM:-sandbox-local}"
STATE_DIR="${SANDBOX_STATE_DIR:-$REPO_ROOT/.sandbox}"
SANDBOX_KUBECONFIG="${SANDBOX_KUBECONFIG:-$STATE_DIR/kubeconfig}"
SANDBOX_KUBE_CONTEXT="${SANDBOX_KUBE_CONTEXT:-sandbox-local}"
SANDBOX_POD_CIDR="${SANDBOX_POD_CIDR:-10.244.0.0/16}"
API_PORT="${SANDBOX_LOCAL_API_PORT:-18448}"
SANDBOX_CONTROL_PLANE_PORT="${SANDBOX_LOCAL_CONTROL_PLANE_PORT:-18080}"
VM_CPUS="${SANDBOX_LOCAL_CPUS:-4}"
VM_MEMORY_GIB="${SANDBOX_LOCAL_MEMORY_GIB:-6}"
VM_DISK_GIB="${SANDBOX_LOCAL_DISK_GIB:-60}"
# Fixed local tags: imagePullPolicy Never in the manifests matches these names.
PROJECT_IMAGES=(
  sandbox-runtime:0.5.0
  sandbox-file-service:0.3.0
  sandbox-control-plane:0.7.0
  sandbox-console:0.1.0
)

usage() {
  printf '%s\n' \
    "usage: scripts/local-cluster.sh <up|status|down|destroy|kubeconfig>" \
    "" \
    "up          create/reuse the Lima VM and deploy the local gVisor profile" \
    "status      show nodes, RuntimeClass, and sandbox workloads" \
    "down        stop the VM without deleting its disk" \
    "destroy     delete the VM disk, the state directory, and local images" \
    "kubeconfig  print the isolated kubeconfig path"
}

require_tools() {
  local command_name
  for command_name in docker limactl kubectl helm openssl python3; do
    command -v "$command_name" >/dev/null 2>&1 || {
      printf 'missing required command: %s\n' "$command_name" >&2
      return 1
    }
  done
  docker info >/dev/null
  local port
  for port in "$API_PORT" "$SANDBOX_CONTROL_PLANE_PORT"; do
    [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1024 && port <= 65535 )) || {
      printf 'invalid Lima host port: %s\n' "$port" >&2
      return 1
    }
  done
  [ "$API_PORT" != "$SANDBOX_CONTROL_PLANE_PORT" ] || {
    printf 'SANDBOX_LOCAL_API_PORT and SANDBOX_LOCAL_CONTROL_PLANE_PORT must differ\n' >&2
    return 1
  }
}

vm_exists() {
  limactl list --format '{{.Name}}' 2>/dev/null | grep -Fx "$VM_NAME" >/dev/null
}

vm_running() {
  [ "$(limactl list "$VM_NAME" --format '{{.Status}}' 2>/dev/null)" = Running ]
}

configured_host_port() {
  local guest_port="$1"
  limactl list "$VM_NAME" --json \
    | python3 -c 'import json,sys
guest = int(sys.argv[1])
config = json.load(sys.stdin).get("config", {})
matches = [p.get("hostPort") for p in config.get("portForwards", []) if p.get("guestPort") == guest]
print(matches[0] if matches else "")' "$guest_port"
}

verify_existing_ports() {
  local configured_api configured_control_plane
  configured_api="$(configured_host_port 6443)"
  configured_control_plane="$(configured_host_port 30080)"
  if [ "$configured_api" != "$API_PORT" ] || [ "$configured_control_plane" != "$SANDBOX_CONTROL_PLANE_PORT" ]; then
    printf '%s\n' \
      "Lima VM $VM_NAME already uses API/Control Plane ports ${configured_api:-unknown}/${configured_control_plane:-unknown};" \
      "requested $API_PORT/$SANDBOX_CONTROL_PLANE_PORT. Reuse its configured ports or choose a new SANDBOX_LOCAL_VM." >&2
    return 1
  fi
}

guest_ip() {
  limactl shell "$VM_NAME" -- sh -c \
    "ip -4 route get 1.1.1.1 | sed -n 's/.* src \\([^ ]*\\).*/\\1/p'"
}

write_kubeconfig() {
  local api_ip="$1"
  mkdir -p "$STATE_DIR"
  umask 077
  limactl shell "$VM_NAME" -- sudo cat /etc/kubernetes/admin.conf >"$SANDBOX_KUBECONFIG"
  KUBECONFIG="$SANDBOX_KUBECONFIG" kubectl config rename-context \
    kubernetes-admin@kubernetes "$SANDBOX_KUBE_CONTEXT" >/dev/null 2>&1 || true
  KUBECONFIG="$SANDBOX_KUBECONFIG" kubectl config set-cluster kubernetes \
    --server="https://127.0.0.1:$API_PORT" \
    --tls-server-name="$api_ip" >/dev/null
  chmod 600 "$SANDBOX_KUBECONFIG"
}

ensure_cluster() {
  local api_ip
  if ! vm_exists; then
    limactl start --name "$VM_NAME" --tty=false \
      --cpus "$VM_CPUS" --memory "$VM_MEMORY_GIB" --disk "$VM_DISK_GIB" \
      --set ".portForwards[0].hostPort = $API_PORT" \
      --set ".portForwards[1].hostPort = $SANDBOX_CONTROL_PLANE_PORT" \
      "$SCRIPT_DIR/local-cluster.yaml"
  else
    verify_existing_ports
  fi
  if ! vm_running; then
    limactl start --tty=false "$VM_NAME"
  fi
  api_ip="$(guest_ip)"
  [ -n "$api_ip" ] || { echo 'cannot determine Lima guest IP' >&2; return 1; }
  if ! limactl shell "$VM_NAME" -- sudo test -s /etc/kubernetes/admin.conf; then
    limactl shell "$VM_NAME" -- sudo kubeadm init \
      --apiserver-advertise-address="$api_ip" \
      --apiserver-cert-extra-sans=127.0.0.1 \
      --pod-network-cidr="$SANDBOX_POD_CIDR" \
      --skip-token-print 2>&1 \
      | sed -E 's/[a-z0-9]{6}\.[a-z0-9]{16}/[redacted-bootstrap-token]/g'
    # The bootstrap-token phase also creates kubelet API RBAC used by logs and
    # exec. Keep that phase, but remove its short-lived join credential because
    # this profile is intentionally single-node.
    limactl shell "$VM_NAME" -- sudo bash -c \
      'kubeadm token list 2>/dev/null | awk "NR > 1 {print \$1}" | xargs -r kubeadm token delete >/dev/null'
  fi
  write_kubeconfig "$api_ip"
  export KUBECONFIG="$SANDBOX_KUBECONFIG"
  bash "$SCRIPT_DIR/install-cilium-kubeadm.sh" "$SANDBOX_KUBE_CONTEXT"
  kubectl --context "$SANDBOX_KUBE_CONTEXT" taint nodes --all \
    node-role.kubernetes.io/control-plane- >/dev/null 2>&1 || true
  kubectl --context "$SANDBOX_KUBE_CONTEXT" label nodes --all \
    sandbox.hullwork.com/node-role=runtime --overwrite >/dev/null
  bash "$SCRIPT_DIR/install-gvisor-kubeadm.sh" "$VM_NAME"
  kubectl --context "$SANDBOX_KUBE_CONTEXT" wait node --all \
    --for=condition=Ready --timeout=5m
}

build_and_load_images() {
  make -C "$REPO_ROOT" images
  local image
  for image in \
    "${PROJECT_IMAGES[@]}" \
    registry.k8s.io/metrics-server/metrics-server@sha256:d9862115e7c7881280d3d75ca26bda8ffc0fc213315979575bf23ce9826205c0; do
    docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"
    docker save "$image" | limactl shell "$VM_NAME" -- sudo ctr \
      --namespace k8s.io images import - >/dev/null
  done
}

deploy_ceph_rgw() {
  export KUBECONFIG="$SANDBOX_KUBECONFIG"
  kubectl --context "$SANDBOX_KUBE_CONTEXT" create namespace rook-ceph \
    --dry-run=client -o yaml \
    | kubectl --context "$SANDBOX_KUBE_CONTEXT" label --local -f - \
        --dry-run=client -o yaml \
        pod-security.kubernetes.io/enforce=privileged \
        pod-security.kubernetes.io/enforce-version=latest \
    | kubectl --context "$SANDBOX_KUBE_CONTEXT" apply -f -
  # Pulled and checksummed like the Cilium chart, not installed straight from
  # the repository index: the index is fetched over HTTPS but nothing else
  # ties the tarball that arrives to the one that was reviewed.
  local rook_version="${ROOK_CEPH_CHART_VERSION:-v1.20.6}"
  local rook_sha256="${ROOK_CEPH_CHART_SHA256:-83a16ee19dd8d621df4159504b33585d80da1bf7ed83c734a9e8d4828c724353}"
  local rook_dir rook_chart rook_actual
  rook_dir="$(mktemp -d "${TMPDIR:-/tmp}/sandbox-rook-chart.XXXXXX")"
  helm pull rook-ceph --repo https://charts.rook.io/release \
    --version "$rook_version" --destination "$rook_dir"
  rook_chart="$rook_dir/rook-ceph-$rook_version.tgz"
  rook_actual="$(shasum -a 256 "$rook_chart" | awk '{print $1}')"
  if [ "$rook_actual" != "$rook_sha256" ]; then
    printf 'Rook chart checksum mismatch: expected %s, got %s\n' "$rook_sha256" "$rook_actual" >&2
    rm -rf "$rook_dir"
    return 1
  fi
  helm upgrade --install rook-ceph "$rook_chart" \
    --namespace rook-ceph \
    --kubeconfig "$SANDBOX_KUBECONFIG" \
    --set csi.installCsiOperator=true \
    --set enableDiscoveryDaemon=false \
    --set allowLoopDevices=true \
    --wait --timeout 10m
  rm -rf "$rook_dir"
  kubectl --context "$SANDBOX_KUBE_CONTEXT" apply -f "$REPO_ROOT/rook/loop-device.yaml"
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n rook-ceph rollout status \
    daemonset/rook-local-loop-device --timeout=5m
  kubectl --context "$SANDBOX_KUBE_CONTEXT" apply -f "$REPO_ROOT/rook/cluster-local.yaml"
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n rook-ceph wait \
    --for=condition=Ready cephcluster/rook-ceph --timeout=20m
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n rook-ceph wait \
    "--for=jsonpath={.status.phase}=Ready" cephobjectstore/object-store --timeout=15m
  local rook_secret=rook-ceph-object-user-object-store-sandbox-runtime
  local access_key secret_key
  for _ in $(seq 1 120); do
    access_key="$(kubectl --context "$SANDBOX_KUBE_CONTEXT" -n rook-ceph get secret \
      "$rook_secret" -o jsonpath='{.data.AccessKey}' 2>/dev/null || true)"
    secret_key="$(kubectl --context "$SANDBOX_KUBE_CONTEXT" -n rook-ceph get secret \
      "$rook_secret" -o jsonpath='{.data.SecretKey}' 2>/dev/null || true)"
    [ -n "$access_key" ] && [ -n "$secret_key" ] && break
    sleep 2
  done
  [ -n "$access_key" ] && [ -n "$secret_key" ] || {
    echo "Rook did not create the Sandbox RGW credential" >&2
    return 1
  }
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n sandbox-system create secret generic \
    object-store-credentials \
    --from-literal="access-key=$(printf '%s' "$access_key" | base64 --decode)" \
    --from-literal="secret-key=$(printf '%s' "$secret_key" | base64 --decode)" \
    --dry-run=client -o yaml \
    | kubectl --context "$SANDBOX_KUBE_CONTEXT" apply -f -
}

deploy_local_profile() {
  export KUBECONFIG="$SANDBOX_KUBECONFIG"
  kubectl --context "$SANDBOX_KUBE_CONTEXT" apply -f \
    https://raw.githubusercontent.com/rancher/local-path-provisioner/c4fdcada94c2e632cd7d9231e73406d554eb40e2/deploy/local-path-storage.yaml
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n local-path-storage rollout status \
    deployment/local-path-provisioner --timeout=3m
  kubectl --context "$SANDBOX_KUBE_CONTEXT" apply -f "$REPO_ROOT/k8s/namespaces.yaml"
  SANDBOX_KUBE_CONTEXT="$SANDBOX_KUBE_CONTEXT" bash "$SCRIPT_DIR/bootstrap-local-secrets.sh"
  deploy_ceph_rgw
  # Both initialization Jobs are idempotent, but Job pod templates are
  # immutable. Recreate them so upgrades do not fail on an old completed Job.
  kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace sandbox-system \
    delete job sandbox-object-store-init --ignore-not-found --wait=true
  kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace sandbox-workloads \
    delete job sandbox-workspace-init --ignore-not-found --wait=true
  kubectl --context "$SANDBOX_KUBE_CONTEXT" apply -k "$REPO_ROOT/overlays/local"
  # Project images use fixed local tags and imagePullPolicy Never. Importing a
  # new image under the same tag does not change the Deployment template, so an
  # existing Pod would otherwise keep running the previous image indefinitely.
  kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace sandbox-system \
    rollout restart deployment/sandbox-control-plane deployment/sandbox-console
  kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace sandbox-workloads \
    rollout restart deployment/sandbox-volume
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n sandbox-system wait \
    --for=condition=complete job/sandbox-object-store-init --timeout=3m
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n sandbox-workloads wait \
    --for=condition=complete job/sandbox-workspace-init --timeout=3m
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n sandbox-system rollout status \
    deployment/sandbox-control-plane --timeout=3m
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n sandbox-workloads rollout status \
    deployment/sandbox-volume --timeout=3m
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n sandbox-system rollout status \
    deployment/sandbox-console --timeout=3m
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n kube-system rollout status \
    deployment/metrics-server --timeout=3m
  kubectl --context "$SANDBOX_KUBE_CONTEXT" wait --for=condition=Available \
    apiservice/v1beta1.metrics.k8s.io --timeout=3m
}

destroy_local_profile() {
  # Every step is best effort so a partially created environment can still be
  # torn down; each line says what it removes before removing it.
  case "$STATE_DIR" in
    /|"$HOME"|"$REPO_ROOT")
      printf 'refusing to delete state directory %s\n' "$STATE_DIR" >&2
      return 1
      ;;
  esac
  printf 'deleting Lima VM %s\n' "$VM_NAME"
  limactl delete -f "$VM_NAME" || true
  printf 'deleting state directory %s\n' "$STATE_DIR"
  rm -rf "$STATE_DIR" || true
  local image
  for image in "${PROJECT_IMAGES[@]}"; do
    printf 'deleting local image %s\n' "$image"
    docker rmi "$image" || true
  done
}

case "${1:-}" in
  up)
    require_tools
    ensure_cluster
    build_and_load_images
    deploy_local_profile
    printf 'Control Plane: http://127.0.0.1:%s\nKubeconfig: %s\n' \
      "$SANDBOX_CONTROL_PLANE_PORT" "$SANDBOX_KUBECONFIG"
    ;;
  status)
    export KUBECONFIG="$SANDBOX_KUBECONFIG"
    kubectl --context "$SANDBOX_KUBE_CONTEXT" get nodes
    kubectl --context "$SANDBOX_KUBE_CONTEXT" get runtimeclass gvisor
    kubectl --context "$SANDBOX_KUBE_CONTEXT" -n kube-system get deployment metrics-server
    kubectl --context "$SANDBOX_KUBE_CONTEXT" get apiservice v1beta1.metrics.k8s.io
    kubectl --context "$SANDBOX_KUBE_CONTEXT" get pods -A -l app.kubernetes.io/managed-by=sandbox-controller
    ;;
  down)
    vm_exists && limactl stop "$VM_NAME"
    ;;
  destroy)
    destroy_local_profile
    ;;
  kubeconfig)
    printf '%s\n' "$SANDBOX_KUBECONFIG"
    ;;
  *) usage >&2; exit 2 ;;
esac
