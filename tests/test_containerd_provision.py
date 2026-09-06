from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "containerd_migration", ROOT / "scripts/migrate-local-containerd-provision.py",
)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class ContainerdProvisionTests(unittest.TestCase):
    def test_repeated_provision_preserves_gvisor_and_operator_settings(self):
        for version, plugin in ((2, "io.containerd.grpc.v1.cri"), (3, "io.containerd.cri.v1.runtime"), (4, "io.containerd.cri.v1.runtime")):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / "config.toml"
                log = root / "commands.log"
                tools = root / "bin"
                tools.mkdir()
                defaults = f'version = {version}\nSystemdCgroup = false\n'
                self._tool(tools, "containerd", f"import os\nfrom pathlib import Path\nwith open(os.environ['PROVISION_LOG'],'a') as f: f.write('generate\\n')\nprint({defaults!r},end='')\n")
                self._tool(tools, "systemctl", "import os,sys\nwith open(os.environ['PROVISION_LOG'],'a') as f: f.write('systemctl '+' '.join(sys.argv[1:])+'\\n')\n")
                # The guest is Linux; preserve real sed behavior on a macOS
                # test host, whose -i spelling requires an empty suffix.
                self._tool(tools, "sed", "import os,sys\na=sys.argv[1:]\nif sys.platform=='darwin' and a[0]=='-i': a.insert(1,'')\nos.execv('/usr/bin/sed',['sed',*a])\n")
                script = "set -eu\n" + MIGRATION.current_block().replace("/etc/containerd/config.toml", str(config))
                environment = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "PROVISION_LOG": str(log)}
                self._provision(script, environment)
                self.assertEqual(config.read_text(), defaults.replace("false", "true"))
                custom = (
                    config.read_text() + f'\n[plugins."{plugin}".containerd.runtimes.runsc]\n'
                    'runtime_type = "io.containerd.runsc.v1"\n'
                    '# Operator-owned registry configuration\n[registry]\nconfig_path = "/etc/containerd/certs.d"\n'
                )
                config.write_text(custom)
                before = config.stat().st_mtime_ns
                self._provision(script, environment)
                self._provision(script, environment)
                self.assertEqual(config.read_text(), custom)
                self.assertEqual(config.stat().st_mtime_ns, before)
                commands = log.read_text().splitlines()
                self.assertEqual(commands.count("generate"), 1)
                self.assertEqual(commands.count("systemctl restart containerd"), 1)
                self.assertEqual(commands.count("systemctl start containerd"), 2)

    def test_existing_cgroup_fix_preserves_runtime_and_restarts_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            log = root / "commands.log"
            content = 'version = 2\nSystemdCgroup = false\n[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]\nruntime_type = "io.containerd.runsc.v1"\n'
            config.write_text(content)
            self._tool(root, "containerd", "raise SystemExit('Existing configuration must not be regenerated')\n")
            self._tool(root, "systemctl", "import os,sys\nwith open(os.environ['PROVISION_LOG'],'a') as f: f.write(' '.join(sys.argv[1:])+'\\n')\n")
            self._tool(root, "sed", "import os,sys\na=sys.argv[1:]\nif sys.platform=='darwin' and a[0]=='-i': a.insert(1,'')\nos.execv('/usr/bin/sed',['sed',*a])\n")
            script = "set -eu\n" + MIGRATION.current_block().replace("/etc/containerd/config.toml", str(config))
            environment = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "PROVISION_LOG": str(log)}
            self._provision(script, environment)
            self._provision(script, environment)
            self.assertEqual(config.read_text(), content.replace("false", "true"))
            self.assertEqual(log.read_text().splitlines().count("restart containerd"), 1)

    @staticmethod
    def _tool(directory, name, source):
        path = directory / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o755)

    def _provision(self, script, environment):
        result = subprocess.run(["sh", "-c", script], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ContainerdMigrationTests(unittest.TestCase):
    def _record(self, status="Stopped"):
        return {"name": "fixture", "status": status, "config": {
            "cpus": 7, "memory": "11GiB", "mounts": [{"location": "/operator-owned"}],
            "portForwards": [{"guestPort": 6443, "hostPort": 18448}],
            "provision": [{"mode": "system", "script": "before\n" + MIGRATION.LEGACY + "after\n"},
                          {"mode": "user", "script": "echo operator-script\n"}],
        }}

    def test_migration_preserves_unrelated_configuration_and_is_idempotent(self):
        record = self._record()
        original = copy.deepcopy(record)
        updates = MIGRATION.provision_updates(record["config"])
        self.assertEqual(len(updates), 1)
        self.assertEqual(record, original, "planning must not mutate the source")
        index, script = updates[0]
        self.assertEqual(index, 0)
        self.assertEqual(script, "before\n" + MIGRATION.current_block() + "after\n")
        record["config"]["provision"][index]["script"] = script
        self.assertEqual(MIGRATION.provision_updates(record["config"]), [])

    def test_unknown_legacy_changes_are_not_overwritten(self):
        for script in (
            MIGRATION.LEGACY.replace("systemctl restart", "custom-systemctl restart"),
            MIGRATION.LEGACY.replace("default >", "default > "),
        ):
            with self.subTest(script=script):
                record = self._record()
                record["config"]["provision"][0]["script"] = script
                with self.assertRaisesRegex(ValueError, "operator changes"):
                    MIGRATION.provision_updates(record["config"])

    def test_running_vm_requires_explicit_maintenance_stop(self):
        with patch.object(sys, "argv", ["migration", "fixture"]), patch.object(MIGRATION.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, json.dumps(self._record("Running")), "")
            with self.assertRaisesRegex(RuntimeError, "stop the VM"):
                MIGRATION.main()
            self.assertEqual(run.call_count, 1, "must not edit, stop, or start the VM")

    def test_check_does_not_edit_a_running_vm(self):
        with patch.object(sys, "argv", ["migration", "fixture", "--check"]), patch.object(MIGRATION.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, json.dumps(self._record("Running")), "")
            self.assertEqual(MIGRATION.main(), 0)
            self.assertEqual(run.call_count, 1)

    def test_stopped_vm_edit_is_verified_by_readback(self):
        record = self._record()
        saved = copy.deepcopy(record)
        saved["config"]["provision"][0]["script"] = MIGRATION.provision_updates(record["config"])[0][1]
        responses = [subprocess.CompletedProcess([], 0, json.dumps(value), "") for value in (record, {}, saved)]
        with patch.object(sys, "argv", ["migration", "fixture"]), patch.object(MIGRATION.subprocess, "run", side_effect=responses) as run:
            self.assertEqual(MIGRATION.main(), 0)
            edit = run.call_args_list[1].args[0]
            self.assertEqual(edit[:4], ["limactl", "edit", "fixture", "--tty=false"])
            self.assertEqual(edit[4], "--set")
            self.assertEqual(json.loads(edit[5].split(" = ", 1)[1]), saved["config"]["provision"][0]["script"])
            self.assertEqual(len(run.call_args_list), 3)

    def test_failed_readback_is_not_reported_as_migrated(self):
        record = self._record()
        responses = [subprocess.CompletedProcess([], 0, json.dumps(value), "") for value in (record, {}, record)]
        with patch.object(sys, "argv", ["migration", "fixture"]), patch.object(MIGRATION.subprocess, "run", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "differs"):
                MIGRATION.main()


if __name__ == "__main__":
    unittest.main()
