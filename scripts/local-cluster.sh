#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONTROL_PLANE_VM="${SANDBOX_LOCAL_VM:-sandbox-local}"
RUNTIME_VM_PREFIX="${SANDBOX_LOCAL_WORKER_PREFIX:-${CONTROL_PLANE_VM}-w}"
RUNTIME_WORKER_COUNT="${SANDBOX_LOCAL_WORKER_COUNT:-1}"
CLUSTER_NETWORK="${SANDBOX_LOCAL_NETWORK:-${CONTROL_PLANE_VM}-net}"
CLUSTER_NETWORK_GATEWAY="${SANDBOX_LOCAL_NETWORK_GATEWAY:-}"
STATE_DIR="${SANDBOX_STATE_DIR:-$REPO_ROOT/.sandbox}"
SANDBOX_KUBECONFIG="${SANDBOX_KUBECONFIG:-$STATE_DIR/kubeconfig}"
SANDBOX_KUBE_CONTEXT="${SANDBOX_KUBE_CONTEXT:-sandbox-local}"
SANDBOX_POD_CIDR="${SANDBOX_POD_CIDR:-10.244.0.0/16}"
API_PORT="${SANDBOX_LOCAL_API_PORT:-18448}"
SANDBOX_CONTROL_PLANE_PORT="${SANDBOX_LOCAL_CONTROL_PLANE_PORT:-18080}"
CONTROL_PLANE_CPUS="${SANDBOX_LOCAL_CPUS:-4}"
CONTROL_PLANE_MEMORY_GIB="${SANDBOX_LOCAL_MEMORY_GIB:-6}"
CONTROL_PLANE_DISK_GIB="${SANDBOX_LOCAL_DISK_GIB:-60}"
RUNTIME_CPUS="${SANDBOX_LOCAL_WORKER_CPUS:-4}"
RUNTIME_MEMORY_GIB="${SANDBOX_LOCAL_WORKER_MEMORY_GIB:-4}"
RUNTIME_DISK_GIB="${SANDBOX_LOCAL_WORKER_DISK_GIB:-30}"
IMAGE_BUILD_JOBS="${SANDBOX_IMAGE_BUILD_JOBS:-4}"
CAPACITY_RESTART_REQUIRED=0
# Fixed local tags: imagePullPolicy Never in the manifests matches these names.
PROJECT_IMAGES=(
  sandbox-runtime:0.5.0
  sandbox-file-service:0.3.0
  sandbox-control-plane:0.7.0
  sandbox-console:0.1.0
)

usage() {
  printf '%s\n' \
    "usage: scripts/local-cluster.sh <up|status|down|destroy|kubeconfig|scale-workers COUNT>" \
    "" \
    "up          create/reuse the 1+N Lima/kubeadm cluster and deploy gVisor" \
    "status      show nodes, RuntimeClass, and sandbox workloads" \
    "down        stop the control plane and every worker without deleting disks" \
    "destroy     delete all profile VM disks, the state directory, and local images" \
    "kubeconfig  print the isolated kubeconfig path" \
    "scale-workers COUNT  safely scale the Runtime worker pool (COUNT may be 0)"
}

runtime_vm_name() {
  printf '%s%s\n' "$RUNTIME_VM_PREFIX" "$1"
}

runtime_vms() {
  limactl list --format '{{.Name}}' 2>/dev/null \
    | awk -v prefix="$RUNTIME_VM_PREFIX" \
      'index($0, prefix) == 1 && substr($0, length(prefix) + 1) ~ /^[1-9][0-9]*$/ { print }'
}

network_exists() {
  limactl network list --json 2>/dev/null | python3 -c 'import json,sys
wanted = sys.argv[1]
raise SystemExit(0 if any(json.loads(line).get("name") == wanted for line in sys.stdin if line.strip()) else 1)' \
    "$CLUSTER_NETWORK"
}

ensure_cluster_network() {
  if ! network_exists; then
    local gateway
    gateway="$(limactl network list --json 2>/dev/null | python3 -c 'import ipaddress,json,sys
requested = sys.argv[1]
if requested:
    interface = ipaddress.ip_interface(requested)
    if interface.version != 4 or interface.network.prefixlen != 24:
        raise SystemExit("SANDBOX_LOCAL_NETWORK_GATEWAY must be an IPv4 /24 gateway")
    print(str(interface))
    raise SystemExit(0)
used = {item.get("gateway") for line in sys.stdin if line.strip() for item in (json.loads(line),)}
for octet in range(108, 224):
    candidate = f"192.168.{octet}.1"
    if candidate not in used:
        print(f"{candidate}/24")
        raise SystemExit(0)
raise SystemExit("no free Lima user-v2 subnet in 192.168.108.0/24..192.168.223.0/24")' \
      "$CLUSTER_NETWORK_GATEWAY")"
    limactl network create "$CLUSTER_NETWORK" --mode=user-v2 --gateway="$gateway"
  fi
}

configured_vm_network() {
  local vm="$1"
  limactl list "$vm" --json | python3 -c 'import json,sys
networks = json.load(sys.stdin).get("config", {}).get("networks") or []
print(next((item.get("lima", "") for item in networks if item.get("lima")), ""))'
}

verify_vm_network() {
  local vm="$1"
  local configured
  configured="$(configured_vm_network "$vm")"
  if [ "$configured" != "$CLUSTER_NETWORK" ]; then
    printf '%s\n' \
      "Lima VM $vm uses network ${configured:-isolated-usernet}, not the shared cluster network $CLUSTER_NETWORK." \
      "The installer will not recreate an existing VM. Preserve any data, run 'make destroy-local', then rerun 'make quickstart'." >&2
    return 1
  fi
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
    if ! [[ "$port" =~ ^[0-9]+$ ]] || ((port < 1024 || port > 65535)); then
      printf 'invalid Lima host port: %s\n' "$port" >&2
      return 1
    fi
  done
  [ "$API_PORT" != "$SANDBOX_CONTROL_PLANE_PORT" ] || {
    printf 'SANDBOX_LOCAL_API_PORT and SANDBOX_LOCAL_CONTROL_PLANE_PORT must differ\n' >&2
    return 1
  }
  [[ "$RUNTIME_WORKER_COUNT" =~ ^[1-9][0-9]*$ ]] || {
    printf 'SANDBOX_LOCAL_WORKER_COUNT must be a positive integer for up, got %s\n' \
      "$RUNTIME_WORKER_COUNT" >&2
    return 1
  }
}

vm_exists() {
  local vm="$1"
  limactl list --format '{{.Name}}' 2>/dev/null | grep -Fx "$vm" >/dev/null
}

vm_running() {
  local vm="$1"
  [ "$(limactl list "$vm" --format '{{.Status}}' 2>/dev/null)" = Running ]
}

configured_host_port() {
  local guest_port="$1"
  limactl list "$CONTROL_PLANE_VM" --json \
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
      "Lima VM $CONTROL_PLANE_VM already uses API/Control Plane ports ${configured_api:-unknown}/${configured_control_plane:-unknown};" \
      "requested $API_PORT/$SANDBOX_CONTROL_PLANE_PORT. Reuse its configured ports or choose a new SANDBOX_LOCAL_VM." >&2
    return 1
  fi
}

guest_ip() {
  local vm="$1"
  limactl shell "$vm" -- sh -c \
    "ip -4 route get 1.1.1.1 | sed -n 's/.* src \\([^ ]*\\).*/\\1/p'"
}

guest_hostname() {
  local vm="$1"
  limactl list "$vm" --json | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["hostname"])'
}

remove_worker_node_registration() {
  local runtime_node="$1"
  # The VM is already stopped (or known to be stopped), so force-removing its
  # remaining DaemonSet Pods cannot create duplicate processes. Node deletion
  # alone leaves those Pods counted as unavailable for several minutes, which
  # makes a subsequent Helm --wait look hung.
  kubectl --context "$SANDBOX_KUBE_CONTEXT" delete node "$runtime_node" \
    --ignore-not-found --wait=true >/dev/null
  kubectl --context "$SANDBOX_KUBE_CONTEXT" delete pod --all-namespaces \
    --field-selector="spec.nodeName=$runtime_node" \
    --grace-period=0 --force --wait=false >/dev/null 2>&1 || true
}

remove_stale_worker_nodes() {
  local runtime_vm runtime_node
  while read -r runtime_vm; do
    [ -n "$runtime_vm" ] || continue
    vm_running "$runtime_vm" && continue
    runtime_node="$(guest_hostname "$runtime_vm")"
    if kubectl --context "$SANDBOX_KUBE_CONTEXT" get node "$runtime_node" \
      >/dev/null 2>&1; then
      printf 'removing stopped worker %s from the live Kubernetes node set\n' \
        "$runtime_node"
    fi
    remove_worker_node_registration "$runtime_node"
  done < <(runtime_vms)
}

write_kubeconfig() {
  local api_ip="$1"
  mkdir -p "$STATE_DIR"
  umask 077
  limactl shell "$CONTROL_PLANE_VM" -- sudo cat /etc/kubernetes/admin.conf >"$SANDBOX_KUBECONFIG"
  KUBECONFIG="$SANDBOX_KUBECONFIG" kubectl config rename-context \
    kubernetes-admin@kubernetes "$SANDBOX_KUBE_CONTEXT" >/dev/null 2>&1 || true
  KUBECONFIG="$SANDBOX_KUBECONFIG" kubectl config set-cluster kubernetes \
    --server="https://127.0.0.1:$API_PORT" \
    --tls-server-name="$api_ip" >/dev/null
  chmod 600 "$SANDBOX_KUBECONFIG"
}

wait_for_kubernetes_api() {
  local attempt
  for ((attempt = 1; attempt <= 90; attempt++)); do
    if kubectl --context "$SANDBOX_KUBE_CONTEXT" get --raw=/readyz \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  printf 'Kubernetes API did not become ready within 180 seconds\n' >&2
  return 1
}

ensure_kubeadm_prerequisites() {
  local vm="$1"
  if limactl shell "$vm" -- sh -c \
    'command -v kubeadm >/dev/null && command -v kubelet >/dev/null && command -v ctr >/dev/null'; then
    return 0
  fi
  printf 'Kubernetes prerequisites are incomplete on %s; retrying the idempotent provisioner\n' "$vm"
  # Lima currently reports a VM as ready even when a system provision script
  # exits non-zero. Re-running the generated script turns that silent partial
  # boot into either a recovered node or an explicit installer failure.
  limactl shell "$vm" -- sudo sh /mnt/lima-cidata/provision.system/00000000
  limactl shell "$vm" -- sh -c \
    'command -v kubeadm >/dev/null && command -v kubelet >/dev/null && command -v ctr >/dev/null'
}

ensure_runtime_worker() {
  local worker_index="$1"
  local runtime_vm runtime_node join_command
  runtime_vm="$(runtime_vm_name "$worker_index")"
  if ! vm_exists "$runtime_vm"; then
    limactl start --name "$runtime_vm" --tty=false \
      --cpus "$RUNTIME_CPUS" --memory "$RUNTIME_MEMORY_GIB" \
      --disk "$RUNTIME_DISK_GIB" \
      --network "lima:$CLUSTER_NETWORK" \
      --set '.portForwards = []' \
      "$SCRIPT_DIR/local-cluster.yaml"
  else
    verify_vm_network "$runtime_vm"
    if ! vm_running "$runtime_vm"; then
      limactl start --tty=false "$runtime_vm"
    fi
  fi
  ensure_kubeadm_prerequisites "$runtime_vm"
  runtime_node="$(guest_hostname "$runtime_vm")"
  if ! kubectl --context "$SANDBOX_KUBE_CONTEXT" get node "$runtime_node" >/dev/null 2>&1; then
    if limactl shell "$runtime_vm" -- sudo test -s /etc/kubernetes/kubelet.conf; then
      limactl shell "$runtime_vm" -- sudo kubeadm reset --force >/dev/null
    fi
    join_command="$(limactl shell "$CONTROL_PLANE_VM" -- sudo kubeadm token create \
      --ttl 30m --print-join-command)"
    limactl shell "$runtime_vm" -- sudo sh -c "$join_command"
    # The command is intentionally single-quoted for evaluation in the guest.
    # shellcheck disable=SC2016
    limactl shell "$CONTROL_PLANE_VM" -- sudo bash -c \
      'kubeadm token list 2>/dev/null | awk "NR > 1 {print \$1}" | xargs -r kubeadm token delete >/dev/null'
  fi
  # Keep the node unschedulable until gVisor is installed and the Runtime image
  # has been imported. A Runtime Pod selects the role label below, so labeling
  # an uncordoned node first would create a race with image/runtime readiness.
  kubectl --context "$SANDBOX_KUBE_CONTEXT" cordon "$runtime_node" >/dev/null
  kubectl --context "$SANDBOX_KUBE_CONTEXT" label node "$runtime_node" \
    sandbox.hullwork.com/node-role=runtime --overwrite >/dev/null
  kubectl --context "$SANDBOX_KUBE_CONTEXT" taint node "$runtime_node" \
    sandbox.hullwork.com/node-role=runtime:NoSchedule --overwrite >/dev/null
  bash "$SCRIPT_DIR/install-gvisor-kubeadm.sh" "$runtime_vm"
  kubectl --context "$SANDBOX_KUBE_CONTEXT" wait node "$runtime_node" \
    --for=condition=Ready --timeout=5m
}

ensure_cluster() {
  local api_ip control_plane_node worker_index
  if ! vm_exists "$CONTROL_PLANE_VM"; then
    ensure_cluster_network
    limactl start --name "$CONTROL_PLANE_VM" --tty=false \
      --cpus "$CONTROL_PLANE_CPUS" --memory "$CONTROL_PLANE_MEMORY_GIB" \
      --disk "$CONTROL_PLANE_DISK_GIB" \
      --network "lima:$CLUSTER_NETWORK" \
      --set ".portForwards[0].hostPort = $API_PORT" \
      --set ".portForwards[1].hostPort = $SANDBOX_CONTROL_PLANE_PORT" \
      "$SCRIPT_DIR/local-cluster.yaml"
  else
    verify_vm_network "$CONTROL_PLANE_VM"
    verify_existing_ports
  fi
  if ! vm_running "$CONTROL_PLANE_VM"; then
    limactl start --tty=false "$CONTROL_PLANE_VM"
  fi
  ensure_kubeadm_prerequisites "$CONTROL_PLANE_VM"
  api_ip="$(guest_ip "$CONTROL_PLANE_VM")"
  [ -n "$api_ip" ] || { echo 'cannot determine Lima guest IP' >&2; return 1; }
  if ! limactl shell "$CONTROL_PLANE_VM" -- sudo test -s /etc/kubernetes/admin.conf; then
    limactl shell "$CONTROL_PLANE_VM" -- sudo kubeadm init \
      --apiserver-advertise-address="$api_ip" \
      --apiserver-cert-extra-sans=127.0.0.1 \
      --pod-network-cidr="$SANDBOX_POD_CIDR" \
      --skip-token-print 2>&1 \
      | sed -E 's/[a-z0-9]{6}\.[a-z0-9]{16}/[redacted-bootstrap-token]/g'
  fi
  write_kubeconfig "$api_ip"
  export KUBECONFIG="$SANDBOX_KUBECONFIG"
  wait_for_kubernetes_api
  # A retained, stopped Lima disk must not leave a NotReady Kubernetes Node
  # behind: Helm waits on node-wide DaemonSets such as Cilium and CSI. Delete
  # only that registration; ensure_runtime_worker reuses the disk and lets the
  # kubelet register again when the worker is requested later.
  remove_stale_worker_nodes
  bash "$SCRIPT_DIR/install-cilium-kubeadm.sh" "$SANDBOX_KUBE_CONTEXT"
  control_plane_node="$(guest_hostname "$CONTROL_PLANE_VM")"
  kubectl --context "$SANDBOX_KUBE_CONTEXT" taint node "$control_plane_node" \
    node-role.kubernetes.io/control-plane- >/dev/null 2>&1 || true
  kubectl --context "$SANDBOX_KUBE_CONTEXT" label node "$control_plane_node" \
    sandbox.hullwork.com/node-role=system --overwrite >/dev/null
  kubectl --context "$SANDBOX_KUBE_CONTEXT" wait node "$control_plane_node" \
    --for=condition=Ready --timeout=5m
  for ((worker_index = 1; worker_index <= RUNTIME_WORKER_COUNT; worker_index++)); do
    ensure_runtime_worker "$worker_index"
  done
}

build_images() {
  [[ "$IMAGE_BUILD_JOBS" =~ ^[1-9][0-9]*$ ]] || {
    printf 'SANDBOX_IMAGE_BUILD_JOBS must be a positive integer, got %s\n' \
      "$IMAGE_BUILD_JOBS" >&2
    return 1
  }
  make -C "$REPO_ROOT" --no-print-directory -j "$IMAGE_BUILD_JOBS" images
}

load_image() {
  local image="$1"
  local vm="$2"
  printf '  loading %s into %s\n' "$image" "$vm"
  docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"
  docker save "$image" | limactl shell "$vm" -- sudo ctr \
    --namespace k8s.io images import - >/dev/null
}

load_images() {
  # System images never need to enter an untrusted Runtime worker. The Control
  # Plane image also serves the volume-agent role, which stays on the trusted
  # system node. Keeping the placement list explicit makes image transfer part
  # of the same topology contract as the Kubernetes scheduling rules.
  load_image sandbox-control-plane:0.7.0 "$CONTROL_PLANE_VM"
  load_image sandbox-console:0.1.0 "$CONTROL_PLANE_VM"
  load_image sandbox-file-service:0.3.0 "$CONTROL_PLANE_VM"
  load_image \
    registry.k8s.io/metrics-server/metrics-server@sha256:d9862115e7c7881280d3d75ca26bda8ffc0fc213315979575bf23ce9826205c0 \
    "$CONTROL_PLANE_VM"
  local worker_index
  for ((worker_index = 1; worker_index <= RUNTIME_WORKER_COUNT; worker_index++)); do
    local runtime_vm runtime_node
    runtime_vm="$(runtime_vm_name "$worker_index")"
    load_image sandbox-runtime:0.5.0 "$runtime_vm"
    runtime_node="$(guest_hostname "$runtime_vm")"
    kubectl --context "$SANDBOX_KUBE_CONTEXT" uncordon "$runtime_node" >/dev/null
  done
}

wait_for_ceph_csi() {
  kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace rook-ceph rollout status \
    deployment/rook-ceph.cephfs.csi.ceph.com-ctrlplugin --timeout=10m
  kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace rook-ceph rollout status \
    daemonset/rook-ceph.cephfs.csi.ceph.com-nodeplugin --timeout=10m
  kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace rook-ceph rollout status \
    deployment/rook-ceph.rbd.csi.ceph.com-ctrlplugin --timeout=10m
  kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace rook-ceph rollout status \
    daemonset/rook-ceph.rbd.csi.ceph.com-nodeplugin --timeout=10m
}

reconcile_workspace_storage_class() {
  local current_mounter referenced_pvs referenced_pvcs
  current_mounter="$(kubectl --context "$SANDBOX_KUBE_CONTEXT" get storageclass sandbox-rwx \
    -o jsonpath='{.parameters.mounter}' 2>/dev/null || true)"
  if [ -z "$current_mounter" ]; then
    if ! kubectl --context "$SANDBOX_KUBE_CONTEXT" get storageclass sandbox-rwx \
      >/dev/null 2>&1; then
      return 0
    fi
  elif [ "$current_mounter" = fuse ]; then
    return 0
  fi

  # StorageClass parameters are immutable. Recreate an obsolete class only
  # when it is provably unused; existing volumes require an explicit migration
  # so an installer rerun can never delete or strand user data.
  referenced_pvs="$(kubectl --context "$SANDBOX_KUBE_CONTEXT" get pv \
    -o jsonpath='{range .items[?(@.spec.storageClassName=="sandbox-rwx")]}{.metadata.name}{"\n"}{end}')"
  referenced_pvcs="$(kubectl --context "$SANDBOX_KUBE_CONTEXT" get pvc --all-namespaces \
    -o jsonpath='{range .items[?(@.spec.storageClassName=="sandbox-rwx")]}{.metadata.namespace}{"/"}{.metadata.name}{"\n"}{end}')"
  if [ -n "$referenced_pvs" ] || [ -n "$referenced_pvcs" ]; then
    printf '%s\n' \
      "existing sandbox-rwx volumes use an obsolete immutable StorageClass configuration (mounter=${current_mounter:-unset})." \
      "The installer will not replace it while PVs or PVCs exist. Back up and migrate those volumes before rerunning quickstart." \
      "PVs: ${referenced_pvs:-none}" \
      "PVCs: ${referenced_pvcs:-none}" >&2
    return 1
  fi
  printf 'recreating unused sandbox-rwx StorageClass with mounter=fuse\n'
  kubectl --context "$SANDBOX_KUBE_CONTEXT" delete storageclass sandbox-rwx
}

deploy_ceph_rgw() {
  export KUBECONFIG="$SANDBOX_KUBECONFIG"
  local control_plane_node
  control_plane_node="$(guest_hostname "$CONTROL_PLANE_VM")"
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
  # v3.17.0 embeds Ceph 20.2.1 and cannot decode the AES256K keys generated by
  # security-fixed Ceph 20.2.4. v3.17.1 is the upstream compatibility release.
  local ceph_csi_image="${CEPH_CSI_IMAGE:-v3.17.1@sha256:0b62db8afc9b174619186c87084a8952157daa719415c598603be22b63cc1293}"
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
    --set-string "csi.cephcsi.tag=$ceph_csi_image" \
    --set enableDiscoveryDaemon=false \
    --set allowLoopDevices=true \
    --wait --timeout 10m
  rm -rf "$rook_dir"
  # Rook 1.20 delegates actual Driver resources to a second chart. Installing
  # only rook-ceph leaves StorageClasses present but no CSI provisioner, so a
  # clean quickstart would wait forever on its first PVC.
  local csi_version="${CEPH_CSI_DRIVERS_CHART_VERSION:-1.0.4}"
  local csi_sha256="${CEPH_CSI_DRIVERS_CHART_SHA256:-76a1787baa7d62232eb073ab8260a455a016c02b59aae584a47be6791f05994b}"
  local csi_dir csi_chart csi_actual
  csi_dir="$(mktemp -d "${TMPDIR:-/tmp}/sandbox-ceph-csi-chart.XXXXXX")"
  helm pull ceph-csi-drivers --repo https://ceph.github.io/ceph-csi-operator \
    --version "$csi_version" --destination "$csi_dir"
  csi_chart="$csi_dir/ceph-csi-drivers-$csi_version.tgz"
  csi_actual="$(shasum -a 256 "$csi_chart" | awk '{print $1}')"
  if [ "$csi_actual" != "$csi_sha256" ]; then
    printf 'Ceph-CSI drivers chart checksum mismatch: expected %s, got %s\n' \
      "$csi_sha256" "$csi_actual" >&2
    rm -rf "$csi_dir"
    return 1
  fi
  helm upgrade --install ceph-csi-drivers "$csi_chart" \
    --namespace rook-ceph \
    --kubeconfig "$SANDBOX_KUBECONFIG" \
    --values "$REPO_ROOT/rook/ceph-csi-drivers-values.yaml" \
    --wait --timeout 10m
  rm -rf "$csi_dir"
  # The drivers chart creates CRs; their operator-owned Deployments and
  # DaemonSets are asynchronous and are not covered by Helm's --wait.
  wait_for_ceph_csi
  kubectl --context "$SANDBOX_KUBE_CONTEXT" apply -f "$REPO_ROOT/rook/loop-device.yaml"
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n rook-ceph rollout status \
    daemonset/rook-local-loop-device --timeout=5m
  reconcile_workspace_storage_class
  sed "s/__SANDBOX_CONTROL_PLANE_NODE__/$control_plane_node/g" \
    "$REPO_ROOT/rook/cluster-local.yaml" \
    | kubectl --context "$SANDBOX_KUBE_CONTEXT" apply -f -
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n rook-ceph wait \
    --for=condition=Ready cephcluster/rook-ceph --timeout=20m
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n rook-ceph wait \
    "--for=jsonpath={.status.phase}=Ready" cephobjectstore/object-store --timeout=15m
  kubectl --context "$SANDBOX_KUBE_CONTEXT" -n rook-ceph wait \
    "--for=jsonpath={.status.phase}=Ready" cephfilesystem/sandbox-filesystem --timeout=15m
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

deployment_runs_host_image() {
  local namespace="$1"
  local deployment="$2"
  local image="$3"
  local expected_image_id running_image_ids
  expected_image_id="$(docker image inspect "$image" --format '{{.Id}}' 2>/dev/null)" \
    || return 1
  running_image_ids="$(kubectl --context "$SANDBOX_KUBE_CONTEXT" \
    --namespace "$namespace" get pods \
    -l "app.kubernetes.io/name=$deployment" \
    -o jsonpath='{range .items[*].status.containerStatuses[*]}{.imageID}{"\n"}{end}' \
    2>/dev/null)" || return 1
  grep -Fx "$expected_image_id" <<<"$running_image_ids" >/dev/null
}

delete_failed_initialization_jobs() {
  local namespace job failed
  while read -r namespace job; do
    failed="$(kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace "$namespace" \
      get job "$job" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' \
      2>/dev/null || true)"
    if [ "$failed" = True ]; then
      printf 'recreating failed initialization Job %s/%s\n' "$namespace" "$job"
      kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace "$namespace" \
        delete job "$job" --wait=true
    fi
  done <<'EOF'
sandbox-system sandbox-object-store-init
sandbox-workloads sandbox-workspace-init
EOF
}

verify_workspace_storage_compatibility() {
  local storage_class
  storage_class="$(kubectl --context "$SANDBOX_KUBE_CONTEXT" \
    --namespace sandbox-workloads get pvc sandbox-workspaces \
    -o jsonpath='{.spec.storageClassName}' 2>/dev/null || true)"
  if [ -n "$storage_class" ] && [ "$storage_class" != sandbox-rwx ]; then
    printf '%s\n' \
      "existing local Workspace volume uses StorageClass $storage_class, not the scalable sandbox-rwx CephFS class." \
      "The installer will not silently delete or move Workspace data. Back it up, run 'make destroy-local', then rerun 'make quickstart'." >&2
    return 1
  fi
}

assert_pods_on_node() {
  local namespace="$1"
  local selector="$2"
  local expected_node="$3"
  local actual_nodes
  actual_nodes="$(kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace "$namespace" \
    get pods -l "$selector" -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}')"
  [ -n "$actual_nodes" ] || {
    printf 'no Pods found for %s/%s\n' "$namespace" "$selector" >&2
    return 1
  }
  if grep -Fvx "$expected_node" <<<"$actual_nodes" >/dev/null; then
    printf 'Pods for %s/%s are not confined to %s: %s\n' \
      "$namespace" "$selector" "$expected_node" "${actual_nodes//$'\n'/, }" >&2
    return 1
  fi
}

verify_deployed_topology() {
  local control_plane_node runtime_node worker_index
  control_plane_node="$(guest_hostname "$CONTROL_PLANE_VM")"
  assert_pods_on_node sandbox-system \
    app.kubernetes.io/name=sandbox-control-plane "$control_plane_node"
  assert_pods_on_node sandbox-system \
    app.kubernetes.io/name=sandbox-console "$control_plane_node"
  assert_pods_on_node sandbox-workloads \
    app.kubernetes.io/name=sandbox-volume "$control_plane_node"
  verify_workspace_storage_compatibility
  for ((worker_index = 1; worker_index <= RUNTIME_WORKER_COUNT; worker_index++)); do
    runtime_node="$(guest_hostname "$(runtime_vm_name "$worker_index")")"
    [ "$control_plane_node" != "$runtime_node" ]
    [ "$(kubectl --context "$SANDBOX_KUBE_CONTEXT" get node "$runtime_node" \
      -o jsonpath='{.metadata.labels.sandbox\.hullwork\.com/node-role}')" = runtime ]
    [ "$(kubectl --context "$SANDBOX_KUBE_CONTEXT" get node "$runtime_node" \
      -o jsonpath='{.spec.taints[?(@.key=="sandbox.hullwork.com/node-role")].value}')" = runtime ]
  done
  printf 'Topology verified: system=%s, active Runtime workers=%s\n' \
    "$control_plane_node" "$RUNTIME_WORKER_COUNT"
}

scale_local_capacity() {
  local quota_workers max_runtimes pods services requests_cpu requests_memory
  local limits_cpu limits_memory previous_max_runtimes
  # Runtime admission follows the active worker count exactly, including a
  # deliberate scale-to-zero. Keep a one-worker-sized namespace floor for the
  # trusted volume agent and initialization Jobs that also live in
  # sandbox-workloads.
  quota_workers="$RUNTIME_WORKER_COUNT"
  ((quota_workers > 0)) || quota_workers=1
  max_runtimes=$((4 * RUNTIME_WORKER_COUNT))
  pods=$((10 + max_runtimes))
  services=$((8 + max_runtimes))
  requests_cpu=$((2 * quota_workers))
  requests_memory=$((1300 * quota_workers))
  limits_cpu=$((4 * quota_workers))
  limits_memory=$((3328 * quota_workers))
  previous_max_runtimes="$(kubectl --context "$SANDBOX_KUBE_CONTEXT" \
    --namespace sandbox-system get configmap sandbox-tuning \
    -o jsonpath='{.data.SANDBOX_MAX_RUNTIMES}' 2>/dev/null || true)"
  kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace sandbox-system \
    patch configmap sandbox-tuning --type merge \
    -p "{\"data\":{\"SANDBOX_MAX_RUNTIMES\":\"$max_runtimes\",\"SANDBOX_MAX_INFLIGHT_CREATES\":\"$((2 * RUNTIME_WORKER_COUNT))\"}}" >/dev/null
  kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace sandbox-workloads \
    patch resourcequota sandbox-workload-quota --type merge \
    -p "{\"spec\":{\"hard\":{\"pods\":\"$pods\",\"services\":\"$services\",\"requests.cpu\":\"$requests_cpu\",\"requests.memory\":\"${requests_memory}Mi\",\"limits.cpu\":\"$limits_cpu\",\"limits.memory\":\"${limits_memory}Mi\"}}}" >/dev/null
  CAPACITY_RESTART_REQUIRED=0
  [ "$previous_max_runtimes" = "$max_runtimes" ] || CAPACITY_RESTART_REQUIRED=1
  printf 'Local Runtime capacity: %s workers, %s concurrent Runtimes\n' \
    "$RUNTIME_WORKER_COUNT" "$max_runtimes"
}

scale_workers() {
  local desired="$1"
  local runtime_vm runtime_node worker_index active_runtime_pods was_unschedulable
  local -a workers_to_stop=()
  local -a nodes_to_uncordon_on_failure=()
  [[ "$desired" =~ ^[0-9]+$ ]] || {
    printf 'worker count must be a non-negative integer, got %s\n' "$desired" >&2
    return 2
  }
  if ((desired > 0)); then
    SANDBOX_LOCAL_WORKER_COUNT="$desired" bash "$SCRIPT_DIR/dev-doctor.sh"
  fi
  RUNTIME_WORKER_COUNT="$desired"
  ensure_cluster
  if kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace rook-ceph \
      get driver.csi.ceph.io/rook-ceph.cephfs.csi.ceph.com >/dev/null 2>&1; then
    wait_for_ceph_csi
  fi
  if ((desired > 0)); then
    make -C "$REPO_ROOT" --no-print-directory image-runtime
    for ((worker_index = 1; worker_index <= desired; worker_index++)); do
      runtime_vm="$(runtime_vm_name "$worker_index")"
      load_image sandbox-runtime:0.5.0 "$runtime_vm"
      runtime_node="$(guest_hostname "$runtime_vm")"
      kubectl --context "$SANDBOX_KUBE_CONTEXT" uncordon "$runtime_node" >/dev/null
    done
  fi
  # Preflight every target before changing any node. If one target still owns
  # a Runtime, the requested resize is rejected atomically instead of stopping
  # some higher-numbered workers and failing halfway through.
  while read -r runtime_vm; do
    [ -n "$runtime_vm" ] || continue
    worker_index="${runtime_vm#"$RUNTIME_VM_PREFIX"}"
    ((worker_index > desired)) || continue
    if ! vm_running "$runtime_vm"; then
      continue
    fi
    runtime_node="$(guest_hostname "$runtime_vm")"
    active_runtime_pods="$(kubectl --context "$SANDBOX_KUBE_CONTEXT" \
      --namespace sandbox-workloads get pods \
      -l app.kubernetes.io/name=sandbox-runtime \
      --field-selector="spec.nodeName=$runtime_node" --no-headers 2>/dev/null || true)"
    if [ -n "$active_runtime_pods" ]; then
      printf 'refusing to stop %s: active Runtime Pods are still scheduled there\n%s\n' \
        "$runtime_vm" "$active_runtime_pods" >&2
      return 1
    fi
    workers_to_stop+=("$runtime_vm")
  done < <(runtime_vms | sort -Vr)
  # Close the scheduler race after the read-only preflight. New admissions can
  # still happen between that check and cordon, so cordon every target and then
  # check them all again before the first drain/stop.
  # Bash 3.2 on macOS treats an empty array expansion as unset under `set -u`.
  # Keep every expansion behind a length check so scale-up-only operations do
  # not fail after the new workers are already healthy.
  if ((${#workers_to_stop[@]})); then
    for runtime_vm in "${workers_to_stop[@]}"; do
      runtime_node="$(guest_hostname "$runtime_vm")"
      was_unschedulable="$(kubectl --context "$SANDBOX_KUBE_CONTEXT" get node \
        "$runtime_node" -o jsonpath='{.spec.unschedulable}')"
      if [ "$was_unschedulable" != true ]; then
        kubectl --context "$SANDBOX_KUBE_CONTEXT" cordon "$runtime_node" >/dev/null
        nodes_to_uncordon_on_failure+=("$runtime_node")
      fi
    done
    for runtime_vm in "${workers_to_stop[@]}"; do
      runtime_node="$(guest_hostname "$runtime_vm")"
      active_runtime_pods="$(kubectl --context "$SANDBOX_KUBE_CONTEXT" \
        --namespace sandbox-workloads get pods \
        -l app.kubernetes.io/name=sandbox-runtime \
        --field-selector="spec.nodeName=$runtime_node" --no-headers 2>/dev/null || true)"
      if [ -n "$active_runtime_pods" ]; then
        local node_to_uncordon
        if ((${#nodes_to_uncordon_on_failure[@]})); then
          for node_to_uncordon in "${nodes_to_uncordon_on_failure[@]}"; do
            kubectl --context "$SANDBOX_KUBE_CONTEXT" uncordon "$node_to_uncordon" \
              >/dev/null 2>&1 || true
          done
        fi
        printf 'refusing to stop %s: a Runtime Pod reached the node during scale-down preflight\n%s\n' \
          "$runtime_vm" "$active_runtime_pods" >&2
        return 1
      fi
    done
    for runtime_vm in "${workers_to_stop[@]}"; do
      runtime_node="$(guest_hostname "$runtime_vm")"
      kubectl --context "$SANDBOX_KUBE_CONTEXT" drain "$runtime_node" \
        --ignore-daemonsets --delete-emptydir-data --force --timeout=3m >/dev/null
      limactl stop "$runtime_vm"
      remove_worker_node_registration "$runtime_node"
    done
  fi
  if kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace sandbox-system \
      get configmap sandbox-tuning >/dev/null 2>&1; then
    scale_local_capacity
    if ((CAPACITY_RESTART_REQUIRED)); then
      kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace sandbox-system \
        rollout restart deployment/sandbox-control-plane >/dev/null
      kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace sandbox-system \
        rollout status deployment/sandbox-control-plane --timeout=4m
    fi
  else
    printf 'Control Plane is not deployed yet; run make up-local to apply capacity.\n'
  fi
  printf 'Runtime worker pool scaled to %s active node(s)\n' "$desired"
}

apply_local_overlay() {
  local output result
  output="$(mktemp "${TMPDIR:-/tmp}/sandbox-kustomize.XXXXXX")"
  set +e
  kubectl --context "$SANDBOX_KUBE_CONTEXT" apply \
    -k "$REPO_ROOT/overlays/local" 2>&1 | tee "$output"
  result="${PIPESTATUS[0]}"
  set -e
  if [ "$result" -ne 0 ] && grep -q 'field is immutable' "$output"; then
    printf 'initialization Job template changed; recreating the immutable Jobs\n'
    kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace sandbox-system \
      delete job sandbox-object-store-init --ignore-not-found --wait=true
    kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace sandbox-workloads \
      delete job sandbox-workspace-init --ignore-not-found --wait=true
    kubectl --context "$SANDBOX_KUBE_CONTEXT" apply -k "$REPO_ROOT/overlays/local"
    result=$?
  fi
  rm -f "$output"
  return "$result"
}

deploy_local_profile() {
  export KUBECONFIG="$SANDBOX_KUBECONFIG"
  local system_deployments_to_restart=()
  local restart_volume_deployment=0
  local deployment image
  verify_workspace_storage_compatibility
  for deployment in sandbox-control-plane sandbox-console; do
    if [ "$deployment" = sandbox-control-plane ]; then
      image=sandbox-control-plane:0.7.0
    else
      image=sandbox-console:0.1.0
    fi
    if kubectl --context "$SANDBOX_KUBE_CONTEXT" -n sandbox-system get \
      deployment "$deployment" >/dev/null 2>&1 \
      && ! deployment_runs_host_image sandbox-system "$deployment" "$image"; then
      system_deployments_to_restart+=("deployment/$deployment")
    fi
  done
  if kubectl --context "$SANDBOX_KUBE_CONTEXT" -n sandbox-workloads get \
    deployment sandbox-volume >/dev/null 2>&1 \
    && ! deployment_runs_host_image sandbox-workloads sandbox-volume \
      sandbox-control-plane:0.7.0; then
    restart_volume_deployment=1
  fi
  kubectl --context "$SANDBOX_KUBE_CONTEXT" apply -f "$REPO_ROOT/k8s/namespaces.yaml"
  SANDBOX_KUBE_CONTEXT="$SANDBOX_KUBE_CONTEXT" bash "$SCRIPT_DIR/bootstrap-local-secrets.sh"
  deploy_ceph_rgw
  # Completed initialization Jobs are evidence that this idempotent work is
  # already done. Preserve them on a normal rerun; recreating both after every
  # Rook reconciliation raced RGW and made a healthy profile fail. Failed Jobs
  # are retried, while an actual template change takes the immutable-Job retry
  # path in apply_local_overlay.
  delete_failed_initialization_jobs
  apply_local_overlay
  scale_local_capacity
  if ((CAPACITY_RESTART_REQUIRED)) \
    && [[ ! " ${system_deployments_to_restart[*]} " =~ " deployment/sandbox-control-plane " ]]; then
    system_deployments_to_restart+=("deployment/sandbox-control-plane")
  fi
  # Project images use fixed local tags and imagePullPolicy Never. Importing a
  # new image under the same tag does not change the Deployment template, so an
  # existing Pod would otherwise keep running the previous image indefinitely.
  # Fixed tags need an explicit restart on an update. A fresh install has just
  # created these Pods, however, and restarting the Control Plane immediately
  # triggers its 210-second graceful shutdown. That made clean installs time
  # out before the replacement Pod could become Ready.
  if ((${#system_deployments_to_restart[@]})); then
    kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace sandbox-system \
      rollout restart "${system_deployments_to_restart[@]}"
  fi
  if ((restart_volume_deployment)); then
    kubectl --context "$SANDBOX_KUBE_CONTEXT" --namespace sandbox-workloads \
      rollout restart deployment/sandbox-volume
  fi
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
  verify_deployed_topology
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
  local runtime_vm
  while read -r runtime_vm; do
    [ -n "$runtime_vm" ] || continue
    printf 'deleting Lima VM %s\n' "$runtime_vm"
    limactl delete -f "$runtime_vm" || true
  done < <(runtime_vms)
  printf 'deleting Lima VM %s\n' "$CONTROL_PLANE_VM"
  limactl delete -f "$CONTROL_PLANE_VM" || true
  if network_exists; then
    printf 'deleting Lima network %s\n' "$CLUSTER_NETWORK"
    limactl network delete --force "$CLUSTER_NETWORK" || true
  fi
  printf 'deleting state directory %s\n' "$STATE_DIR"
  rm -rf "$STATE_DIR" || true
  local image
  for image in "${PROJECT_IMAGES[@]}"; do
    printf 'deleting local image %s\n' "$image"
    docker rmi "$image" || true
  done
}

UP_PHASE=0
UP_PHASE_COUNT=5
run_up_phase() {
  local label="$1"
  shift
  local started="$SECONDS"
  UP_PHASE=$((UP_PHASE + 1))
  printf '\n[%s/%s] %s\n' "$UP_PHASE" "$UP_PHASE_COUNT" "$label"
  # A function invoked directly as an `if` condition inherits Bash's
  # suppression of `set -e`, so an early kubectl failure can be hidden by a
  # later successful command. Run the phase in its own errexit-enabled shell
  # and inspect that shell's status instead.
  set +e
  (set -e; "$@")
  local result=$?
  set -e
  if [ "$result" -eq 0 ]; then
    printf '[%s/%s] complete in %ss\n' \
      "$UP_PHASE" "$UP_PHASE_COUNT" "$((SECONDS - started))"
  else
    printf '[%s/%s] failed after %ss: %s\n' \
      "$UP_PHASE" "$UP_PHASE_COUNT" "$((SECONDS - started))" "$label" >&2
    if [ -s "$SANDBOX_KUBECONFIG" ]; then
      printf 'Current cluster state:\n' >&2
      KUBECONFIG="$SANDBOX_KUBECONFIG" kubectl \
        --context "$SANDBOX_KUBE_CONTEXT" get pods -A >&2 || true
    fi
    return "$result"
  fi
}

case "${1:-}" in
  up)
    run_up_phase 'checking required tools and ports' require_tools
    run_up_phase 'creating or resuming the control plane and Runtime worker pool' ensure_cluster
    run_up_phase "building 4 project images ($IMAGE_BUILD_JOBS parallel jobs)" build_images
    run_up_phase 'loading role-scoped images into Kubernetes nodes' load_images
    run_up_phase 'deploying storage, Control Plane, Runtime services, and Console' deploy_local_profile
    printf 'Control Plane: http://127.0.0.1:%s\nKubeconfig: %s\n' \
      "$SANDBOX_CONTROL_PLANE_PORT" "$SANDBOX_KUBECONFIG"
    ;;
  status)
    export KUBECONFIG="$SANDBOX_KUBECONFIG"
    kubectl --context "$SANDBOX_KUBE_CONTEXT" get nodes \
      -L sandbox.hullwork.com/node-role
    kubectl --context "$SANDBOX_KUBE_CONTEXT" get runtimeclass gvisor
    kubectl --context "$SANDBOX_KUBE_CONTEXT" -n kube-system get deployment metrics-server
    kubectl --context "$SANDBOX_KUBE_CONTEXT" get apiservice v1beta1.metrics.k8s.io
    kubectl --context "$SANDBOX_KUBE_CONTEXT" get pods -A -l app.kubernetes.io/managed-by=sandbox-controller
    ;;
  down)
    while read -r runtime_vm; do
      [ -n "$runtime_vm" ] || continue
      vm_running "$runtime_vm" && limactl stop "$runtime_vm"
    done < <(runtime_vms | sort -Vr)
    vm_exists "$CONTROL_PLANE_VM" && limactl stop "$CONTROL_PLANE_VM"
    ;;
  destroy)
    destroy_local_profile
    ;;
  kubeconfig)
    printf '%s\n' "$SANDBOX_KUBECONFIG"
    ;;
  scale-workers)
    [ "$#" -eq 2 ] || { usage >&2; exit 2; }
    scale_workers "$2"
    ;;
  *) usage >&2; exit 2 ;;
esac
