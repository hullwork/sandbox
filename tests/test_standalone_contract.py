from __future__ import annotations

import ast
import pathlib
import re
import unittest

import workspace_contract


ROOT = pathlib.Path(__file__).resolve().parents[1]


def assigned_default(path: pathlib.Path, variable: str, environment: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == variable for target in node.targets):
            continue
        call = node.value
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "int"
            and call.args
            and isinstance(call.args[0], ast.Call)
            and isinstance(call.args[0].func, ast.Attribute)
            and call.args[0].func.attr == "getenv"
        ):
            args = call.args[0].args
            if len(args) == 2 and ast.literal_eval(args[0]) == environment:
                return ast.literal_eval(args[1])
    raise AssertionError(f"cannot find {variable} default")


class StandaloneContractTests(unittest.TestCase):
    def test_parent_specific_contracts_are_absent(self) -> None:
        # The parent's environment is a namespace, not a fixed list of names:
        # it defines 252 distinct AGENT_* and 170 distinct SITES_* variables.
        # Banning two complete names checked almost nothing - "AGENT_SOURCE_DIR"
        # matched zero of those 252 from the day it was written, so the gate was
        # green because it never looked, and "SITE_SOURCE_DIR" was singular while
        # the parent's own namespace is the plural SITES_*. Ban the prefixes.
        self.assertEqual(self.prefix_offenders(("AGENT_", "SITES_", "SITE_")), [])
        banned = (
            "github.com/hullwork/agent",
            "github.com/hullwork/site",
            "Vercel",
            "runtime_identity",
            "automation-postgres",
            "infra/scripts/",
            "sandbox/README.md",
        )
        self.assertEqual(self.marker_offenders(banned), [])

    def test_parent_specific_messages_are_absent(self) -> None:
        # User-facing text must not send people to modules that only exist in
        # the parent repository.
        banned = (
            "core." + "run",
            "core._" + "workspace",
            "sites/" + "mcp",
            "see infra bootstrap",
        )
        self.assertEqual(self.marker_offenders(banned), [])

    @staticmethod
    def reviewed_sources() -> list[pathlib.Path]:
        sources: list[pathlib.Path] = []
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT)
            if (
                not path.is_file()
                or relative == pathlib.Path("tests/test_standalone_contract.py")
                or {".git", ".planning", ".venv", "node_modules", "__pycache__"}.intersection(relative.parts)
                or path.suffix not in {".json", ".md", ".py", ".sh", ".toml", ".ts", ".tsx", ".yaml", ".yml"}
                and path.name not in {"Dockerfile", "Makefile"}
            ):
                continue
            sources.append(path)
        return sources

    @classmethod
    def marker_offenders(cls, banned: tuple[str, ...]) -> list[str]:
        offenders: list[str] = []
        for path in cls.reviewed_sources():
            text = path.read_text(encoding="utf-8")
            for marker in banned:
                if marker in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {marker}")
        return offenders

    @classmethod
    def prefix_offenders(cls, prefixes: tuple[str, ...]) -> list[str]:
        # Anchored at an identifier boundary on purpose. A bare substring search
        # for "AGENT_" also hits this repository's own VOLUME_AGENT_TOKEN and
        # OBJECT_STORE_AGENT_BUCKET, and burying the gate under that many
        # exemptions is how it stops guarding anything.
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])(?:%s)[A-Za-z0-9_]*"
            % "|".join(re.escape(prefix) for prefix in prefixes)
        )
        offenders: list[str] = []
        for path in cls.reviewed_sources():
            for name in sorted(set(pattern.findall(path.read_text(encoding="utf-8")))):
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
        return offenders

    def test_workspace_limits_have_one_python_authority(self) -> None:
        expected = {
            "MAX_FILE_BYTES": 1_000_000,
            "MAX_READ_CHARS": 8_000,
            "MAX_LIST_ENTRIES": 500,
            "MAX_READ_SOURCE_BYTES": 16 * 1024 * 1024,
            "MAX_READ_LINES": 2_000,
        }
        self.assertEqual(
            {name: getattr(workspace_contract, name) for name in expected},
            expected,
        )
        for path in (
            ROOT / "control_plane/Dockerfile",
            ROOT / "runtime/Dockerfile",
            ROOT / "file-service/Dockerfile",
        ):
            self.assertIn(
                "workspace_contract.py",
                path.read_text(encoding="utf-8"),
                str(path.relative_to(ROOT)),
            )

    def test_control_plane_has_an_acyclic_composition_root(self) -> None:
        core = (ROOT / "control_plane/core.py").read_text(encoding="utf-8")
        manifests = (ROOT / "control_plane/manifests.py").read_text(encoding="utf-8")
        dockerfile = (ROOT / "control_plane/Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("if __name__ ==", core)
        self.assertNotIn("from control_plane import core as control_plane", manifests)
        self.assertIn('ENTRYPOINT ["python3", "-m", "control_plane.server"]', dockerfile)

    def test_base_manifests_are_provider_neutral(self) -> None:
        base_files = [
            path
            for path in (ROOT / "k8s").rglob("*")
            if path.is_file() and path.suffix in {".yaml", ".yml"}
        ]
        source = "\n".join(path.read_text(encoding="utf-8") for path in base_files)
        self.assertIn("storageClassName: sandbox-rwx", source)
        self.assertIn("claimName: sandbox-workspaces", source)
        # Overlays are shipped too: a maintainer's private endpoint in any of
        # them is a dead default for everyone else.
        manifests = [
            path
            for directory in ("k8s", "overlays")
            for path in (ROOT / directory).rglob("*")
            if path.is_file() and path.suffix in {".yaml", ".yml"}
        ]
        offenders = [
            f"{path.relative_to(ROOT)}: {marker}"
            for path in manifests
            for marker in ("NFS_SERVER", "amazonaws.com", "aliyuncs.com", "192.168.6.")
            if marker in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])

    def test_local_ceph_store_survives_gateway_restart(self) -> None:
        source = (ROOT / "rook/cluster-local.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("kind: CephCluster", source)
        self.assertIn("useAllDevices: false", source)
        self.assertIn("- name: /dev/loop0", source)
        self.assertNotIn("deviceFilter:", source)
        self.assertIn("preservePoolsOnDelete: true", source)
        self.assertNotIn("minio/minio", source)

    def test_documented_shell_defaults_match_runtime(self) -> None:
        runtime = ROOT / "runtime/runtime_server.py"
        sessions = assigned_default(
            runtime, "MAX_SESSIONS", "SANDBOX_MAX_SHELL_SESSIONS"
        )
        idle = assigned_default(
            runtime,
            "SESSION_IDLE_TTL_SECONDS",
            "SANDBOX_SHELL_SESSION_IDLE_TTL_SECONDS",
        )
        specifications = (ROOT / "docs/SYSTEM_SPECIFICATIONS.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"| Shell sessions | {sessions} per Runtime |", specifications)
        self.assertIn(
            f"| Shell-session idle limit | {int(idle):,} seconds |",
            specifications,
        )

    def test_local_overlay_is_self_contained(self) -> None:
        component = (ROOT / "overlays/local-dev/kustomization.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("kind: Component", component)
        self.assertIn("control-plane-sqlite.yaml", component)
        self.assertNotIn("workspace-storage-local-path.yaml", component)
        self.assertIn("object-store.yaml", component)
        self.assertIn("metrics-server.yaml", component)
        # The local profile must consume that component rather than carry its
        # own copies or point at dependencies outside the cluster.
        local = (ROOT / "overlays/local/kustomization.yaml").read_text(encoding="utf-8")
        self.assertIn("components:\n  - ../local-dev", local)
        self.assertIn("resources:\n  - ../../k8s", local)
        self.assertNotIn("patches:", local)
        self.assertNotIn("control-plane-external.yaml", local)
        self.assertEqual(
            sorted(path.name for path in (ROOT / "overlays/local").iterdir()),
            ["kustomization.yaml"],
        )

    def test_external_dependency_overlay_is_opt_in_and_placeholder_only(self) -> None:
        overlay = ROOT / "overlays/external-deps"
        kustomization = (overlay / "kustomization.yaml").read_text(encoding="utf-8")
        self.assertIn("control-plane-external.yaml", kustomization)
        patch = (overlay / "control-plane-external.yaml").read_text(encoding="utf-8")
        for placeholder in (
            "your-mysql-host.example.com",
            "https://s3.example.com",
            "your-oss-bucket",
            "sandbox-mysql-auth",
            "sandbox-oss-credentials",
        ):
            self.assertIn(placeholder, patch)
        self.assertIn("experimental", patch)
        referrers = [
            str(path.relative_to(ROOT))
            for path in ROOT.rglob("kustomization.yaml")
            if ".git" not in path.parts
            and path.parent != overlay
            and "external-deps" in path.read_text(encoding="utf-8").replace(
                "overlays/external-deps", ""
            )
        ]
        self.assertEqual(referrers, [])
        scripts = "\n".join(
            path.read_text(encoding="utf-8") for path in (ROOT / "scripts").glob("*.sh")
        )
        self.assertNotIn("external-deps", scripts)


if __name__ == "__main__":
    unittest.main()
