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
  if [ "$(uname -s)" = Darwin; then
    printf 'Start Docker Desktop (for example: open -a Docker), wait for it to report Ready, then rerun make doctor.\n' >&2
  else
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

# A new default VM reserves 6 GiB of memory and a 60 GiB disk (sparse)
# (scripts/local-cluster.sh), so a clean install needs substantial headroom.
# Reusing an existing VM only needs enough headroom to build images and roll
# Pods; applying the new-VM threshold after a successful install made retries
# fail precisely because the VM was already consuming those resources.
# Set SANDBOX_DOCTOR_SKIP_RESOURCES=1 to bypass either mode.
if [ "${SANDBOX_DOCTOR_SKIP_RESOURCES:-0}" = 1 ]; then
  printf 'resources  check skipped (SANDBOX_DOCTOR_SKIP_RESOURCES=1)\n'
else
  LIMA_DISK_DIR="${LIMA_HOME:-$HOME/.lima}"
  [ -d "$LIMA_DISK_DIR" ] || LIMA_DISK_DIR="$HOME"
  DEFAULT_MIN_MEMORY_GIB=8
  DEFAULT_MIN_DISK_GIB=35
  RESOURCE_MODE=new-profile
  if limactl list --format '{{.Name}}' 2>/dev/null \
    | grep -Fx "${SANDBOX_LOCAL_VM:-sandbox-local}" >/dev/null; then
    DEFAULT_MIN_MEMORY_GIB=2
    DEFAULT_MIN_DISK_GIB=5
    RESOURCE_MODE=reuse-profile
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
