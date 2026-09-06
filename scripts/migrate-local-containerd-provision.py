#!/usr/bin/env python3
"""Update only the known old containerd provision block of a stopped Lima VM.

Lima stores its own template copy. Editing scripts/local-cluster.yaml alone
does not repair existing VMs. This command never stops or starts a VM and
does not install gVisor or modify the guest filesystem.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import textwrap


BEGIN = "# BEGIN sandbox containerd configuration"
END = "# END sandbox containerd configuration"
LEGACY = """containerd config default >/etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
# apt starts containerd before the generated configuration exists. Restart
# it explicitly so kubelet's CRI cgroup-driver discovery sees systemd from
# its first boot rather than caching containerd's built-in cgroupfs default.
systemctl enable containerd
systemctl restart containerd
"""


def current_block() -> str:
    template = Path(__file__).with_name("local-cluster.yaml").read_text(encoding="utf-8")
    lines = template.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.strip() == BEGIN)
    end = next(i for i, line in enumerate(lines[start:], start) if line.strip() == END)
    return textwrap.dedent("".join(lines[start:end + 1]))


def provision_updates(config: dict) -> list[tuple[int, str]]:
    updates = []
    replacement = current_block()
    for index, provision in enumerate(config.get("provision", [])):
        script = provision.get("script", "")
        if not isinstance(script, str):
            raise ValueError("provision script is not text; review the VM template manually")
        if BEGIN in script:
            if replacement not in script:
                raise ValueError("managed containerd block was customized; review it manually")
            continue
        if "containerd config default" not in script:
            continue
        if script.count(LEGACY) != 1:
            raise ValueError("unknown containerd provision block; refusing to replace operator changes")
        updates.append((index, script.replace(LEGACY, replacement, 1)))
    return updates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vm", help="exact Lima VM name")
    parser.add_argument("--check", action="store_true", help="report migration needs without editing or stopping anything")
    arguments = parser.parse_args()
    result = subprocess.run(["limactl", "list", arguments.vm, "--json"], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("cannot inspect the requested Lima VM")
    record = json.loads(result.stdout)
    if record.get("name") != arguments.vm:
        raise ValueError("Lima did not return the exact requested VM")
    updates = provision_updates(record.get("config", {}))
    if arguments.check:
        print(json.dumps({"vm": arguments.vm, "migration_required": bool(updates), "blocks": len(updates)}))
        return 0
    if not updates:
        print(json.dumps({"vm": arguments.vm, "migrated": False, "reason": "no legacy block"}))
        return 0
    if record.get("status") != "Stopped":
        raise RuntimeError("stop the VM during an approved maintenance window, then rerun migration; no changes made")
    command = ["limactl", "edit", arguments.vm, "--tty=false"]
    for index, script in updates:
        command.extend(["--set", f".provision[{index}].script = {json.dumps(script)}"])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("Lima edit failed; inspect the VM configuration before restarting")
    # Readback proves the saved instance template, not merely our repo file.
    result = subprocess.run(["limactl", "list", arguments.vm, "--json"], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("cannot verify the saved Lima VM configuration")
    saved = json.loads(result.stdout)
    for index, script in updates:
        if saved.get("config", {}).get("provision", [])[index].get("script") != script:
            raise RuntimeError("saved Lima provision differs from the requested migration")
    print(json.dumps({"vm": arguments.vm, "migrated": True, "blocks": len(updates), "verified": True}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, IndexError) as error:
        # Do not echo captured VM metadata or the full script (operator scripts
        # may contain credentials); our errors contain only action guidance.
        raise SystemExit(str(error)) from None
