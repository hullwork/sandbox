"""The Kubernetes label and annotation prefix belongs to this project.

Every label, annotation and node-selector key the platform writes or reads
carries the ``sandbox.hullwork.com/`` prefix. The prefix is a namespace, not an
address: Kubernetes never resolves it. It still has to be a domain the project
controls, because two products writing ``expires-at`` under the same prefix
would read each other's clocks. The previous prefix belonged to a maintainer's
personal domain that was not under the project's control any more.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PREFIX = "sandbox.hullwork.com/"
RETIRED = re.compile(r"\bconvee\.io/")
SCOPE = ("control_plane", "k8s", "overlays", "charts", "scripts", "file-service", "runtime")


def tracked_files() -> list[pathlib.Path]:
    listed = subprocess.run(
        ["git", "ls-files", "--", *SCOPE],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.split()
    return [ROOT / name for name in listed if not name.endswith((".png", ".tgz"))]


class LabelDomainTests(unittest.TestCase):
    def test_the_scan_still_sees_the_prefix(self) -> None:
        hits = sum(
            path.read_text(encoding="utf-8", errors="ignore").count(PREFIX)
            for path in tracked_files()
        )
        self.assertGreater(hits, 20, "the label prefix scan found almost nothing; is the pattern broken?")

    def test_the_retired_prefix_is_gone(self) -> None:
        offenders = [
            path.relative_to(ROOT).as_posix()
            for path in tracked_files()
            if RETIRED.search(path.read_text(encoding="utf-8", errors="ignore"))
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()


class PlacementLabelDocumentationTests(unittest.TestCase):
    """The placement label a reader is told to check must be the real one.

    The scan above deliberately excludes ``docs/``: prose mentions keys it does
    not write. That exemption let the "Runtime Pod stays Pending" check ship as
    ``kubectl get nodes --show-labels | grep sandbox-node`` - a string this
    product never writes, so it printed nothing on a correctly labelled cluster
    and nothing on an unlabelled one. A check that cannot separate the two
    answers "the label is missing" to a reader who is debugging placement.
    """

    @staticmethod
    def deployed_node_selector() -> str:
        manifest = (ROOT / "k8s/control-plane.yaml").read_text(encoding="utf-8")
        match = re.search(
            r"name: SANDBOX_RUNTIME_NODE_SELECTOR\s*\n\s*value: (\S+)", manifest
        )
        assert match, "SANDBOX_RUNTIME_NODE_SELECTOR is no longer set in k8s/control-plane.yaml"
        return match.group(1).strip("\"'")

    def test_the_pending_pod_check_selects_on_the_deployed_label(self) -> None:
        selector = self.deployed_node_selector()
        self.assertTrue(selector.startswith(PREFIX), selector)
        guide = (ROOT / "docs/TROUBLESHOOTING.md").read_text(encoding="utf-8")
        start = guide.index("## Runtime Pod stays `Pending`")
        section = guide[start:guide.index("\n## ", start)]
        self.assertIn(
            f"kubectl get nodes -l {selector}",
            section,
            "the Pending-Pod check must select on the label the Control Plane places by",
        )
