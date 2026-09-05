#!/usr/bin/env bash
set -euo pipefail

required=(docker limactl kubectl helm python3 openssl)
# Lima on Linux has a single vmType, qemu, and boots the VM with the host
# architecture's system emulator, which limactl does not ship: without it
# `limactl start` fails after every check above has passed. macOS uses the
# Virtualization framework instead. shasum is what install-cilium-kubeadm.sh
# verifies the Cilium chart with, and local-cluster.sh the Rook chart.
if [ "$(uname -s)" = Linux ]; then
  required+=("qemu-system-$(uname -m)" shasum)
fi
missing=()

for command_name in "${required[@]}"; do
  if command -v "$command_name" >/dev/null 2>&1; then
    printf '%-10s %s\n' "$command_name" "$(command -v "$command_name")"
  else
    missing+=("$command_name")
  fi
done

if ((${#missing[@]})); then
  printf 'missing required commands: %s\n' "${missing[*]}" >&2
  exit 1
fi

python3 - <<'PY'
import sys

if sys.version_info < (3, 11):
    raise SystemExit(f"Python 3.11+ is required; found {sys.version.split()[0]}")
print(f"python     {sys.version.split()[0]}")
PY

if ! docker info >/dev/null 2>&1; then
  printf 'Docker is installed but its daemon is not reachable.\n' >&2
  if [ "$(uname -s)" = Darwin ]; then
    printf 'Start Docker Desktop (for example: open -a Docker), wait for it to report Ready, then rerun make doctor.\n' >&2
  else
    # Backticks are documentation in this single-quoted message.
    # shellcheck disable=SC2016
    printf 'Start the Docker service and verify `docker context show` points at the intended daemon, then rerun make doctor.\n' >&2
  fi
  exit 1
fi

printf 'docker     daemon reachable\n'

# Lima on Linux needs /dev/kvm for hardware virtualization. Without it the VM
# falls back to QEMU TCG, which boots kubeadm many times slower; warn only,
# because the profile still works. macOS uses its own hypervisor framework.
if [ "$(uname -s)" = Linux ]; then
  if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
    printf 'kvm        /dev/kvm available\n'
  else
    printf 'warning: /dev/kvm is missing or not writable; Lima will fall back to QEMU TCG software emulation, which is extremely slow\n' >&2
  fi
fi

# A new default profile reserves 6 GiB for the control-plane VM and 4 GiB for
# the Runtime worker. Their 60/30 GiB disks are sparse. Reusing both nodes only
# needs enough headroom to build images and roll Pods; adding the worker to a
# legacy single-node profile uses the smaller expansion gate.
# Set SANDBOX_DOCTOR_SKIP_RESOURCES=1 to bypass either mode.
if [ "${SANDBOX_DOCTOR_SKIP_RESOURCES:-0}" = 1 ]; then
  printf 'resources  check skipped (SANDBOX_DOCTOR_SKIP_RESOURCES=1)\n'
else
  LIMA_DISK_DIR="${LIMA_HOME:-$HOME/.lima}"
  [ -d "$LIMA_DISK_DIR" ] || LIMA_DISK_DIR="$HOME"
  CONTROL_PLANE_VM="${SANDBOX_LOCAL_VM:-sandbox-local}"
  RUNTIME_VM_PREFIX="${SANDBOX_LOCAL_WORKER_PREFIX:-${CONTROL_PLANE_VM}-w}"
  RUNTIME_WORKER_COUNT="${SANDBOX_LOCAL_WORKER_COUNT:-1}"
  CONTROL_PLANE_MEMORY_GIB="${SANDBOX_LOCAL_MEMORY_GIB:-6}"
  CONTROL_PLANE_DISK_GIB="${SANDBOX_LOCAL_DISK_GIB:-60}"
  RUNTIME_MEMORY_GIB="${SANDBOX_LOCAL_WORKER_MEMORY_GIB:-4}"
  RUNTIME_DISK_GIB="${SANDBOX_LOCAL_WORKER_DISK_GIB:-30}"
  [[ "$RUNTIME_WORKER_COUNT" =~ ^[1-9][0-9]*$ ]] || {
    printf 'SANDBOX_LOCAL_WORKER_COUNT must be a positive integer, got %s\n' \
      "$RUNTIME_WORKER_COUNT" >&2
    exit 1
  }
  for RESOURCE_VALUE in "$CONTROL_PLANE_MEMORY_GIB" "$CONTROL_PLANE_DISK_GIB" \
    "$RUNTIME_MEMORY_GIB" "$RUNTIME_DISK_GIB"; do
    [[ "$RESOURCE_VALUE" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
      printf 'VM memory and disk values must be positive numbers, got %s\n' \
        "$RESOURCE_VALUE" >&2
      exit 1
    }
  done
  RESOURCE_RESERVES="$(python3 - "$CONTROL_PLANE_MEMORY_GIB" \
    "$CONTROL_PLANE_DISK_GIB" "$RUNTIME_MEMORY_GIB" "$RUNTIME_DISK_GIB" <<'PY'
import sys

control_memory, control_disk, worker_memory, worker_disk = map(float, sys.argv[1:])
if min(control_memory, control_disk, worker_memory, worker_disk) <= 0:
    raise SystemExit("VM memory and disk values must be greater than zero")
print(control_memory, min(control_disk, 25), worker_memory, min(worker_disk, 10))
PY
)"
  read -r CONTROL_MEMORY_RESERVE CONTROL_DISK_RESERVE \
    WORKER_MEMORY_RESERVE WORKER_DISK_RESERVE <<<"$RESOURCE_RESERVES"
  VM_NAMES="$(limactl list --format '{{.Name}}' 2>/dev/null || true)"
  CONTROL_PLANE_EXISTS=0
  grep -Fx "$CONTROL_PLANE_VM" <<<"$VM_NAMES" >/dev/null && CONTROL_PLANE_EXISTS=1
  EXISTING_WORKERS=0
  for WORKER_INDEX in $(seq 1 "$RUNTIME_WORKER_COUNT"); do
    grep -Fx "${RUNTIME_VM_PREFIX}${WORKER_INDEX}" <<<"$VM_NAMES" >/dev/null \
      && EXISTING_WORKERS=$((EXISTING_WORKERS + 1))
  done
  MISSING_WORKERS=$((RUNTIME_WORKER_COUNT - EXISTING_WORKERS))
  if ((CONTROL_PLANE_EXISTS && MISSING_WORKERS == 0)); then
    DEFAULT_MIN_MEMORY_GIB=2
    DEFAULT_MIN_DISK_GIB=5
    RESOURCE_MODE=reuse-profile
  elif ((CONTROL_PLANE_EXISTS)); then
    DEFAULT_MIN_MEMORY_GIB="$(python3 -c "print(2 + $WORKER_MEMORY_RESERVE * $MISSING_WORKERS + 0.5)")"
    DEFAULT_MIN_DISK_GIB="$(python3 -c "print(5 + $WORKER_DISK_RESERVE * $MISSING_WORKERS)")"
    RESOURCE_MODE=expand-worker-pool
  else
    DEFAULT_MIN_MEMORY_GIB="$(python3 -c "print($CONTROL_MEMORY_RESERVE + $WORKER_MEMORY_RESERVE * $MISSING_WORKERS + 0.5)")"
    DEFAULT_MIN_DISK_GIB="$(python3 -c "print($CONTROL_DISK_RESERVE + $WORKER_DISK_RESERVE * $MISSING_WORKERS)")"
    RESOURCE_MODE=new-profile
  fi
  SANDBOX_DOCTOR_DEFAULT_MIN_MEMORY_GIB="$DEFAULT_MIN_MEMORY_GIB" \
  SANDBOX_DOCTOR_DEFAULT_MIN_DISK_GIB="$DEFAULT_MIN_DISK_GIB" \
  SANDBOX_DOCTOR_RESOURCE_MODE="$RESOURCE_MODE" \
    python3 - "$LIMA_DISK_DIR" <<'PY'
import os
import shutil
import subprocess
import sys

GIB = 1024 ** 3
MIN_MEMORY_GIB = float(
    os.environ.get(
        "SANDBOX_DOCTOR_MIN_MEMORY_GIB",
        os.environ["SANDBOX_DOCTOR_DEFAULT_MIN_MEMORY_GIB"],
    )
)
MIN_DISK_GIB = float(
    os.environ.get(
        "SANDBOX_DOCTOR_MIN_DISK_GIB",
        os.environ["SANDBOX_DOCTOR_DEFAULT_MIN_DISK_GIB"],
    )
)


def available_memory_bytes() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            output = subprocess.check_output(
                ["/usr/bin/vm_stat"], text=True, stderr=subprocess.DEVNULL
            )
            page_size = 4096
            first_line = output.splitlines()[0]
            if "page size of" in first_line:
                page_size = int(first_line.split("page size of", 1)[1].split()[0])
            pages = {}
            for line in output.splitlines()[1:]:
                if ":" not in line:
                    continue
                name, value = line.split(":", 1)
                pages[name] = int(value.strip().rstrip("."))
            # Inactive, speculative and purgeable pages are reclaimable under
            # pressure. Counting total physical pages here used to print 32 GiB
            # "available" even when the host had almost no headroom.
            reclaimable = sum(
                pages.get(name, 0)
                for name in (
                    "Pages free",
                    "Pages inactive",
                    "Pages speculative",
                    "Pages purgeable",
                )
            )
            if reclaimable:
                return reclaimable * page_size
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            pass
    return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")


disk_dir = sys.argv[1]
memory_gib = available_memory_bytes() / GIB
disk_gib = shutil.disk_usage(disk_dir).free / GIB
failures = []
print(f"mode       {os.environ['SANDBOX_DOCTOR_RESOURCE_MODE']}")
if memory_gib < MIN_MEMORY_GIB:
    failures.append(f"available memory {memory_gib:.1f} GiB is below the {MIN_MEMORY_GIB:g} GiB minimum")
if disk_gib < MIN_DISK_GIB:
    failures.append(f"free disk {disk_gib:.1f} GiB at {disk_dir} is below the {MIN_DISK_GIB:g} GiB minimum")
print(f"memory     {memory_gib:.1f} GiB available")
print(f"disk       {disk_gib:.1f} GiB free at {disk_dir}")
if failures:
    raise SystemExit(
        "; ".join(failures)
        + ". Free Docker/Lima cache or choose a larger LIMA_HOME volume; "
        + "set SANDBOX_DOCTOR_SKIP_RESOURCES=1 only when you have verified the capacity yourself"
    )
PY
fi

printf 'Sandbox development prerequisites are ready\n'
