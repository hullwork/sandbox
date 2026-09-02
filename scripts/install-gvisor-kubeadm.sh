#!/usr/bin/env bash
# Install gVisor in the local kubeadm VM through Lima's guest shell. The
# generated containerd configuration supports schema versions 2 through 4.
set -euo pipefail

VM="${1:?usage: install-gvisor-kubeadm.sh <vm>}"
#The default pin is only written once: the criterion of "whether release is customized" on the guest side depends on GVISOR_DEFAULT_RELEASE
#Inject it back. heredoc is the quoted version (not expanded), and writing the literal value again is the second drift point——
#That copy does not have any test coverage. When the pin is changed, it is all green, but the guest side will compare it with the old value.
gvisor_default_release="release/20260803.0"
gvisor_release="${GVISOR_RELEASE:-$gvisor_default_release}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_class_file="$repo_root/k8s/platform/runtime-class.yaml"
: "${KUBECONFIG:?set KUBECONFIG to the target kubeadm cluster configuration}"
export KUBECONFIG

for command in limactl kubectl; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "missing required command: ${command}" >&2
    exit 1
  fi
done

#No need to grep -q: under `set -o pipefail` it will close the read end in advance after hitting it, Lima 2.2
#It may therefore exit with SIGPIPE, instead the entire pipeline is non-zero and falsely reports that the VM does not exist.
if ! limactl list -f '{{.Name}}' 2>/dev/null | grep -Fx "$VM" >/dev/null; then
  echo "lima vm not found: $VM" >&2
  exit 1
fi

limactl shell "$VM" -- sudo env \
  GVISOR_RELEASE="$gvisor_release" \
  GVISOR_DEFAULT_RELEASE="$gvisor_default_release" \
  GVISOR_RUNSC_SHA512="${GVISOR_RUNSC_SHA512:-}" \
  GVISOR_SHIM_SHA512="${GVISOR_SHIM_SHA512:-}" \
  bash -seu <<'GUEST_SCRIPT'
config_file=/etc/containerd/config.toml
#The configuration version determines which plug-in family the runsc block hangs: v2 (containerd 1.x) = io.containerd.grpc.v1.cri;
#v3/v4 (containerd 2.x, noble’s v2.3.3 config default writes version = 4) =
#io.containerd.cri.v1.runtime. Old families are ignored by the daemon in v3/v4 configurations.
config_version="$(grep -E "^[[:space:]]*version[[:space:]]*=[[:space:]]*[0-9]+" "$config_file" \
  | head -1 | grep -Eo "[0-9]+")"
case "$config_version" in
  2) cri_plugin="io.containerd.grpc.v1.cri" ;;
  3|4) cri_plugin="io.containerd.cri.v1.runtime" ;;
  *)
    echo "unsupported containerd config version: ${config_version:-missing} (expected 2, 3 or 4) in $config_file" >&2
    exit 1
    ;;
esac
runsc_table="[plugins.\"${cri_plugin}\".containerd.runtimes.runsc]"

needs_restart=0
needs_install=0
if ! command -v runsc >/dev/null 2>&1 || ! command -v containerd-shim-runsc-v1 >/dev/null 2>&1; then
  needs_install=1
elif [ "$GVISOR_RELEASE" != "release/latest" ]; then
  expected_release="release-${GVISOR_RELEASE#release/}"
  installed_release=$(runsc --version | awk "NR == 1 { print \$3 }")
  if [ "$installed_release" != "$expected_release" ]; then
    needs_install=1
  fi
fi

if [ "$needs_install" -eq 1 ]; then
  install_dir=$(mktemp -d /tmp/gvisor-install.XXXXXX)
  trap "rm -rf \"$install_dir\"" EXIT
  architecture=$(uname -m)
  case "$architecture" in
    x86_64)
      default_runsc_sha512="8f87e9a0ed6bec3a50effed65694d2cc71bfe76c0b5e740dcaa58af5af42d83cd441b44d3505abdf3b79e15052dde842e9981fc22c068c05f9ef92e9215becc9"
      default_shim_sha512="e39cc100ef11b6e78a918de8717fb62ace73c5137407dfd92b083264dac9b4f070435fc1f271cd6453b8fdae34cdc01f7fabbe5736bf30af79e389be8ecb424a"
      ;;
    aarch64)
      default_runsc_sha512="4547ead374aceb85d8659492e7b4a176b900ba98f873e4eab7e877968ffc5a01021c4f809bbb047d2d9498779ffab56d05d79ad4fcfb0e10438cb6779504dc49"
      default_shim_sha512="6fdbd646c808d6f8fcf97ebce45244995a117d7d00bab1a1ce3146b1b8dbbc8a11728f715792033f7e62f66af5f766746b230e9570f332a3660a2b2613344dfa"
      ;;
    *)
      echo "unsupported gVisor architecture: $architecture" >&2
      exit 1
      ;;
  esac
  if [ "$GVISOR_RELEASE" != "$GVISOR_DEFAULT_RELEASE" ] \
    && { [ -z "$GVISOR_RUNSC_SHA512" ] || [ -z "$GVISOR_SHIM_SHA512" ]; }; then
    echo "custom GVISOR_RELEASE requires GVISOR_RUNSC_SHA512 and GVISOR_SHIM_SHA512" >&2
    exit 1
  fi
  runsc_sha512="${GVISOR_RUNSC_SHA512:-$default_runsc_sha512}"
  shim_sha512="${GVISOR_SHIM_SHA512:-$default_shim_sha512}"
  release_url="https://storage.googleapis.com/gvisor/releases/${GVISOR_RELEASE}/${architecture}"
  curl -fsSLo "$install_dir/runsc" "$release_url/runsc"
  curl -fsSLo "$install_dir/containerd-shim-runsc-v1" \
    "$release_url/containerd-shim-runsc-v1"
  (
    cd "$install_dir"
    printf "%s  runsc\n" "$runsc_sha512" | sha512sum -c -
    printf "%s  containerd-shim-runsc-v1\n" "$shim_sha512" | sha512sum -c -
  )
  install -m 0755 "$install_dir/runsc" /usr/local/bin/runsc
  install -m 0755 "$install_dir/containerd-shim-runsc-v1" \
    /usr/local/bin/containerd-shim-runsc-v1
  test -x /usr/local/bin/runsc
  test -x /usr/local/bin/containerd-shim-runsc-v1
  needs_restart=1
fi

#The idempotent criterion must check "the header that will take effect in this version": the old family may remain in the v3/v4 configuration.
#runsc block (history script appending, daemon warning ignoring), grep pan matching will misjudge that it is installed.
if ! grep -Fq "$runsc_table" "$config_file"; then
  cp "$config_file" "${config_file}.pre-gvisor"
  printf "\n%s\n  runtime_type = \"io.containerd.runsc.v1\"\n" "$runsc_table" >>"$config_file"
  needs_restart=1
fi

if [ "$needs_restart" -eq 1 ]; then
  systemctl restart containerd
  # Kubernetes 1.36 discovers the cgroup driver through CRI at kubelet start.
  # If containerd was initially started before its generated config existed,
  # kubelet may have cached cgroupfs while runc now expects systemd paths.
  systemctl restart kubelet
fi

runsc --version
GUEST_SCRIPT

kubectl --kubeconfig "$KUBECONFIG" apply -f "$runtime_class_file"
