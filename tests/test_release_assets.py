from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_release_assets", ROOT / "scripts/prepare_release_assets.py"
)
assert SPEC and SPEC.loader
release_assets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_assets)

RELEASE_WORKFLOW = yaml.safe_load(
    (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
)
IMAGE_JOB = RELEASE_WORKFLOW["jobs"]["images"]
RECORD_STEP = next(
    step for step in IMAGE_JOB["steps"]
    if step.get("name") == "Record deployable image identity"
)
DIGEST = "sha256:" + ("a" * 64)


def workflow_image_names() -> list[str]:
    return [entry["name"] for entry in IMAGE_JOB["strategy"]["matrix"]["include"]]


def record_image_identity(component: str, directory: pathlib.Path) -> None:
    """Write image-<component>.json the way the release workflow does.

    The fixture used to be typed out to match the script, and the script was
    read against it: green for a workflow that wrote a different key and a
    different file name, and a release that failed on its final job. So the
    workflow's own step is what writes the file here. With bash and jq on
    PATH that is the literal `run:` block; otherwise the jq filter is lifted
    out of it and evaluated by hand, which still fails on a renamed key.
    """
    environment = {
        **os.environ,
        "COMPONENT": component,
        "IMAGE": f"ghcr.io/hullwork/sandbox-{component}",
        "DIGEST": DIGEST,
    }
    if shutil.which("bash") and shutil.which("jq"):
        subprocess.run(
            ["bash", "-euo", "pipefail", "-c", RECORD_STEP["run"]],
            cwd=directory, env=environment, check=True,
            capture_output=True, text=True,
        )
        return
    # jq -n --arg a "$A" ... '{k:$a,...,immutableRef:($repository+"@"+$digest)}'
    match = re.search(
        r"jq -n((?:\s+--arg \w+ \"\$\w+\"\s*\\?)+)\s*'(\{.*?\})'\s*\\?\s*>\"(.*?)\"",
        RECORD_STEP["run"], re.S,
    )
    assert match, "the jq invocation is not where this fallback looks"
    arguments = dict(re.findall(r'--arg (\w+) "\$(\w+)"', match.group(1)))
    values = {
        name: (
            {"runtime": "images.runtime", "file-service": "images.fileService",
             "control-plane": "images.controlPlane", "console": "images.console"}[component]
            if variable == "value_path" else environment[variable]
        )
        for name, variable in arguments.items()
    }
    record = {}
    for key, expression in re.findall(r"(\w+):(\$\w+|\([^)]*\))", match.group(2)):
        if expression.startswith("$"):
            record[key] = values[expression[1:]]
        else:
            record[key] = "".join(
                values[part[1:]] if part.startswith("$") else json.loads(part)
                for part in re.findall(r'\$\w+|"[^"]*"', expression)
            )
    target = match.group(3).replace("${COMPONENT}", component)
    (directory / target).write_text(json.dumps(record), encoding="utf-8")


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        for component in workflow_image_names():
            record_image_identity(component, self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_script_components_are_the_workflow_matrix_names(self) -> None:
        # Both evidence files are named after matrix.name; a key here that is
        # not a matrix name is a FileNotFoundError on tag day.
        self.assertEqual(
            sorted(release_assets.COMPONENT_IMAGES), sorted(workflow_image_names())
        )

    def test_workflow_written_identities_are_accepted(self) -> None:
        identities = release_assets.load_image_identities(self.root)
        self.assertEqual(
            identities,
            {
                name: f"ghcr.io/hullwork/sandbox-{name}@{DIGEST}"
                for name in workflow_image_names()
            },
        )

    def test_manifest_uses_release_digests_for_workloads_and_runtime_env(self) -> None:
        manifest_input = self.root / "base.yaml"
        manifest_output = self.root / "release.yaml"
        manifest_input.write_text(
            """
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - name: control_plane
          image: sandbox-control-plane:0.7.0
          imagePullPolicy: Never
          env:
            - name: SANDBOX_RUNTIME_IMAGE
              value: sandbox-runtime:0.5.0
        - name: workspace-maintenance
          image: sandbox-file-service:0.3.0
          imagePullPolicy: Never
        - name: console
          image: sandbox-console:0.1.0
          imagePullPolicy: Never
        - name: nfs-provisioner
          image: registry.k8s.io/example/provisioner:v1
          imagePullPolicy: Never
""".lstrip(),
            encoding="utf-8",
        )
        identities = release_assets.load_image_identities(self.root)
        release_assets.render_manifest(manifest_input, manifest_output, identities)
        rendered = manifest_output.read_text(encoding="utf-8")
        self.assertNotIn("sandbox-control-plane:0.7.0", rendered)
        self.assertEqual(rendered.count("@" + DIGEST), 4)
        self.assertNotIn("imagePullPolicy: Never", rendered)
        self.assertEqual(rendered.count("imagePullPolicy: IfNotPresent"), 4)


class ReleaseInventoryTests(unittest.TestCase):
    def test_image_sbom_components_are_in_final_license_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "licenses.json").write_text(
                json.dumps({"format": "v1", "python": [], "npm": [], "images": []}),
                encoding="utf-8",
            )
            for component in workflow_image_names():
                (root / f"image-{component}.cdx.json").write_text(
                    json.dumps(
                        {
                            "bomFormat": "CycloneDX",
                            "components": [
                                {
                                    "type": "library",
                                    "name": f"dependency-{component}",
                                    "version": "1.2.3",
                                    "purl": f"pkg:pypi/dependency-{component}@1.2.3",
                                    "licenses": [{"license": {"id": "MIT"}}],
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
            release_assets.merge_image_sboms(root)
            inventory = json.loads((root / "licenses.json").read_text(encoding="utf-8"))
            self.assertEqual(inventory["format"], "sandbox-license-inventory-v2")
            self.assertEqual(len(inventory["image_components"]), 4)
            self.assertTrue(
                all(item["license"] == "MIT" for item in inventory["image_components"])
            )
            self.assertIn(
                "image:runtime", (root / "licenses.md").read_text(encoding="utf-8")
            )


class ReleaseWorkflowContractTests(unittest.TestCase):
    def test_release_is_gated_by_full_ci_and_public_main_tag(self) -> None:
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_call:", ci)
        self.assertIn("uses: ./.github/workflows/ci.yml", release)
        self.assertIn("needs: ci", release)
        self.assertIn("release tag must be annotated", release)
        self.assertIn("release tag commit must be reachable from origin/main", release)
        self.assertIn("repository is public", release)
        self.assertIn("prepare_release_assets.py", release)
        self.assertIn("--draft", release)

    def test_release_scans_before_promoting_official_image_tags(self) -> None:
        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        scan = release.index("Scan staged image before promotion")
        promote = release.index("Promote scanned digest to official tags")
        sign = release.index("Sign promoted image digest")
        self.assertLess(scan, promote)
        self.assertLess(promote, sign)
        self.assertIn("tags: ${{ steps.image.outputs.staging }}", release)
        self.assertNotIn("Scan published image", release)

    def test_checkout_never_persists_workflow_credentials(self) -> None:
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            source = path.read_text(encoding="utf-8")
            positions = [match.start() for match in re.finditer("actions/checkout@", source)]
            for index, start in enumerate(positions):
                end = positions[index + 1] if index + 1 < len(positions) else len(source)
                step = source[start:end]
                self.assertIn("persist-credentials: false", step, str(path))

    def test_gitleaks_is_checksum_pinned_and_has_only_a_precise_fixture_exception(self) -> None:
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("GITLEAKS_VERSION: 8.30.1", ci)
        self.assertIn(
            "GITLEAKS_SHA256: 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
            ci,
        )
        self.assertNotIn("gitleaks/gitleaks-action", ci)
        self.assertIn("-c .gitleaks.toml", ci)

        # The exemption used to live in .gitleaksignore, keyed by commit SHA and
        # line number. Squashing the history changed both and the entry stopped
        # applying, so the scan failed on main while the file still looked like
        # it was covering the finding. Assert it stays gone: a stale fingerprint
        # is worse than none, because it reads as an exemption that works.
        self.assertFalse((ROOT / ".gitleaksignore").exists())

        config = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
        self.assertIs(config["extend"]["useDefault"], True)
        allowlists = config["allowlists"]
        self.assertEqual(len(allowlists), 1)
        only = allowlists[0]
        # All three clauses together are what makes this a fixture exception
        # rather than a hole: the rule, the one file, and the exact placeholder.
        # Dropping any one of them would exempt something nobody looked at.
        self.assertEqual(only["condition"], "AND")
        self.assertEqual(only["targetRules"], ["generic-api-key"])
        self.assertEqual(only["paths"], [r"^tests/test_api_authorization\.py$"])
        self.assertEqual(only["regexTarget"], "secret")
        self.assertEqual(only["regexes"], ["^sb-0123456789ab$"])

    def test_lima_uses_an_immutable_ubuntu_image_url(self) -> None:
        lima = (ROOT / "scripts/local-cluster.yaml").read_text(encoding="utf-8")
        self.assertNotIn("/current/", lima)
        self.assertEqual(lima.count("/20260814/"), 2)


if __name__ == "__main__":
    unittest.main()
