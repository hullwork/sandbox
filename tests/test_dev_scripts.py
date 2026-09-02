from __future__ import annotations

import importlib.util
import inspect
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCTOR_TOOLS = ("docker", "limactl", "kubectl", "helm", "openssl")


def fake_tool_directory(directory: str) -> str:
    # Stand-ins for the host tools so the doctor's resource gate can be
    # exercised on a machine that has none of them installed.
    for name in DOCTOR_TOOLS:
        tool = pathlib.Path(directory) / name
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool.chmod(0o755)
    return directory


def make_dry_run(target: str) -> str:
    return subprocess.run(
        ["make", "--no-print-directory", "-n", target],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def load_mutation_tool():
    path = ROOT / "scripts/mutation-experiment.py"
    spec = importlib.util.spec_from_file_location("sandbox_mutation_tool", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MutationExperimentToolTests(unittest.TestCase):
    """The tool that validates other guards, validated itself.

    🔴 Every reverse experiment in this suite is evidence only if the tool
    running it cannot report a wrong answer. A harness that silently fails to
    restore, or that mutates on top of an inherited failure, produces readings
    indistinguishable from real ones - so its refusals are exercised here rather
    than trusted. The test runner is injected, so none of this starts a real
    test run.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = load_mutation_tool()

    def setUp(self) -> None:
        self.directory = pathlib.Path(tempfile.mkdtemp(dir=ROOT))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.path = self.directory / "subject.py"
        self.original = "def f():\n    return 1\n\ndef g():\n    return 2\n"
        self.path.write_text(self.original, encoding="utf-8")
        self.relative = str(self.path.relative_to(ROOT))

    def experiment(self, old, new, *, runner):
        return self.tool.experiment(
            "case", self.relative, old, new, "unused.module", run=runner
        )

    def test_a_caught_mutation_reads_as_ok_and_leaves_the_file_intact(self) -> None:
        calls = []

        def runner(module):
            calls.append(self.path.read_text(encoding="utf-8"))
            return 0 if calls[-1] == self.original else 1

        _, verdict, detail = self.experiment(
            "    return 1", "    return 99", runner=runner
        )
        self.assertEqual(verdict, "OK", detail)
        self.assertEqual(detail, "mutated=1 restored=0")
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)

    def test_an_uncaught_mutation_reads_as_a_problem_not_a_pass(self) -> None:
        _, verdict, detail = self.experiment(
            "    return 1", "    return 99", runner=lambda module: 0
        )
        self.assertEqual(verdict, "PROBLEM", detail)

    def test_an_unrevertable_mutation_is_refused(self) -> None:
        # Restoring cannot be verified for a mutation with no content, and the
        # "restored" reading would silently be a second run of the mutated tree.
        for new in ("", "   ", "    return 1"):
            with self.subTest(new=repr(new)):
                _, verdict, _ = self.experiment(
                    "    return 1", new, runner=lambda module: 0
                )
                self.assertEqual(verdict, "REFUSED")

    def test_an_ambiguous_anchor_is_refused(self) -> None:
        # "return" appears twice: the mutation would land somewhere other than
        # intended and the red would be about a different thing.
        _, verdict, detail = self.experiment(
            "    return", "    pass  #", runner=lambda module: 0
        )
        self.assertEqual(verdict, "REFUSED")
        self.assertIn("2 places", detail)

    def test_a_red_baseline_is_refused_rather_than_attributed(self) -> None:
        # 🔴 Otherwise an inherited failure is reported as this change's, and
        # whoever reads it goes off to fix a problem that is not there.
        _, verdict, detail = self.experiment(
            "    return 1", "    return 99", runner=lambda module: 1
        )
        self.assertEqual(verdict, "REFUSED")
        self.assertIn("baseline is already red", detail)

    def test_a_concurrent_writer_aborts_instead_of_being_overwritten(self) -> None:
        """🔴 The restore is a blind write; without this it erases silently."""
        theirs = "# another agent got here first\n"
        calls = []

        def runner(module):
            calls.append(module)
            if len(calls) == 1:
                return 0  # the baseline, which must be green to get this far
            # ...and now somebody else edits the file mid-experiment.
            self.path.write_text(theirs, encoding="utf-8")
            return 1

        _, verdict, detail = self.experiment(
            "    return 1", "    return 99", runner=runner
        )
        self.assertEqual(verdict, "ABORTED", detail)
        self.assertIn("concurrent writer", detail)
        self.assertEqual(
            self.path.read_text(encoding="utf-8"), theirs,
            "the other writer's content must survive",
        )


    def test_the_restore_is_verified_by_hashing_the_file(self) -> None:
        """The one refusal with no in-process failure path to simulate.

        It fires only if writing the original back does not round-trip on the
        filesystem, which cannot be provoked from here without a seam that
        exists purely to be provoked. So what is guarded is its **removal**:
        checked on the comparison node itself rather than by searching the file
        for a string, because a guard that looks for its own literal is beaten
        by any edit that rewrites both at once.

        Stated plainly so the coverage is not overread: the branch is asserted
        to exist, not asserted to work.
        """
        import ast

        source = inspect.getsource(self.tool.experiment)
        compares = [
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Call)
            and isinstance(node.left.func, ast.Name)
            and node.left.func.id == "digest"
        ]
        self.assertEqual(
            len(compares), 1,
            "the restore is no longer verified by comparing content digests",
        )


class DevelopmentScriptTests(unittest.TestCase):
    def test_shell_entrypoints_parse(self) -> None:
        for relative in (
            "scripts/local-cluster.sh",
            "scripts/dev-doctor.sh",
            "scripts/print-dev-token.sh",
            "scripts/bootstrap-local-secrets.sh",
        ):
            with self.subTest(path=relative):
                subprocess.run(["bash", "-n", str(ROOT / relative)], check=True)

    def test_existing_secret_migrates_the_previous_control_token_without_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_kubectl = pathlib.Path(directory) / "kubectl"
            patch_log = pathlib.Path(directory) / "patch.json"
            fake_kubectl.write_text(
                "#!/bin/sh\n"
                "case \" $* \" in\n"
                "  *\".data.control-plane-token}\"*) exit 0 ;;\n"
                "  *\".data.workspace-id-key}\"*) printf 'd29ya3NwYWNlLWtleQ=='; exit 0 ;;\n"
                "  *\".data.token}\"*) printf 'dm9sdW1lLXRva2Vu'; exit 0 ;;\n"
                "  *\"--output=jsonpath={.data.\"*\"-token}\"*) printf 'b2xkLXRva2Vu'; exit 0 ;;\n"
                "  *\" patch secret sandbox-api-credentials \"*)\n"
                "    previous=''\n"
                "    for argument in \"$@\"; do\n"
                "      if [ \"$previous\" = '--patch-file' ]; then cp \"$argument\" \"$FAKE_PATCH_LOG\"; fi\n"
                "      previous=\"$argument\"\n"
                "    done\n"
                "    exit 0 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/bootstrap-local-secrets.sh")],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_PATCH_LOG": str(patch_log),
                },
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                json.loads(patch_log.read_text(encoding="utf-8")),
                {"data": {"control-plane-token": "b2xkLXRva2Vu"}},
            )

    def test_kubectl_make_targets_use_the_isolated_kubeconfig(self) -> None:
        for target in ("dev-token", "control-plane-forward", "console-forward"):
            with self.subTest(target=target):
                self.assertIn("KUBECONFIG=", make_dry_run(target))
                self.assertIn(".sandbox/kubeconfig", make_dry_run(target))

    def test_destroy_local_is_listed_and_delegates_to_the_cluster_script(self) -> None:
        listing = subprocess.run(
            ["make", "--no-print-directory", "help"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("destroy-local", listing)
        self.assertIn("scripts/local-cluster.sh destroy", make_dry_run("destroy-local"))
        script = (ROOT / "scripts/local-cluster.sh").read_text(encoding="utf-8")
        self.assertIn("limactl delete -f", script)
        self.assertIn('rm -rf "$STATE_DIR"', script)
        self.assertIn('docker rmi "$image" || true', script)

    def test_doctor_resource_gate_reports_numbers_and_can_be_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                **os.environ,
                "PATH": f"{fake_tool_directory(directory)}{os.pathsep}{os.environ['PATH']}",
                "SANDBOX_DOCTOR_MIN_MEMORY_GIB": "100000",
                "SANDBOX_DOCTOR_MIN_DISK_GIB": "100000",
            }
            environment.pop("SANDBOX_DOCTOR_SKIP_RESOURCES", None)
            failed = subprocess.run(
                ["bash", str(ROOT / "scripts/dev-doctor.sh")],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
            self.assertRegex(failed.stderr, r"available memory [0-9.]+ GiB is below the 100000 GiB minimum")
            self.assertRegex(failed.stderr, r"free disk [0-9.]+ GiB at .* is below the 100000 GiB minimum")
            self.assertNotIn("prerequisites are ready", failed.stdout)
            skipped = subprocess.run(
                ["bash", str(ROOT / "scripts/dev-doctor.sh")],
                cwd=ROOT,
                env={**environment, "SANDBOX_DOCTOR_SKIP_RESOURCES": "1"},
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("check skipped", skipped.stdout)
            self.assertIn("prerequisites are ready", skipped.stdout)
            if platform.system() == "Linux" and not os.path.exists("/dev/kvm"):
                self.assertIn("/dev/kvm is missing", skipped.stderr)
                self.assertIn("QEMU TCG", skipped.stderr)

    def test_rook_chart_is_pulled_and_checksummed_like_cilium(self) -> None:
        script = (ROOT / "scripts/local-cluster.sh").read_text(encoding="utf-8")
        cilium = (ROOT / "scripts/install-cilium-kubeadm.sh").read_text(encoding="utf-8")
        self.assertNotIn("helm repo add rook-release", script)
        self.assertIn("helm pull rook-ceph --repo https://charts.rook.io/release", script)
        self.assertRegex(script, r"ROOK_CEPH_CHART_SHA256:-[0-9a-f]{64}\}")
        self.assertIn("Rook chart checksum mismatch", script)
        # Same tool as the Cilium installer, so the doctor's shasum check
        # covers both.
        self.assertIn("shasum -a 256", cilium)
        self.assertIn("shasum -a 256", script)

    def test_ceph_image_is_digest_pinned_and_identical_in_both_rook_manifests(self) -> None:
        images = set()
        for relative in ("rook/cluster-local.yaml", "rook/loop-device.yaml"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            found = re.findall(r"image: (quay\.io/ceph/ceph\S+)", text)
            self.assertEqual(len(found), 1, relative)
            self.assertRegex(found[0], r"@sha256:[0-9a-f]{64}$", relative)
            images.add(found[0])
        self.assertEqual(len(images), 1, "the two Rook manifests must move together")

    def test_make_dev_token_outputs_only_the_decoded_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_kubectl = pathlib.Path(directory) / "kubectl"
            fake_kubectl.write_text(
                "#!/bin/sh\n"
                "case \" $* \" in\n"
                "  *\" --context test-context \"*) ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n"
                "printf 'ZGV2ZWxvcG1lbnQtdG9rZW4='\n",
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
            }
            result = subprocess.run(
                [
                    "make",
                    "--no-print-directory",
                    "dev-token",
                    "SANDBOX_KUBE_CONTEXT=test-context",
                ],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.stdout, "development-token\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
