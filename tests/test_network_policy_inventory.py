"""The verifier's policy list has to match what the chart renders.

scripts/verify-network-policy.sh asserts a fixed inventory of NetworkPolicies
before it probes anything, because the probes cannot see one layer of a
redundant pair go missing: deleting sandbox-default-deny on its own leaves them
green, since sandbox-public-egress still refuses the traffic they try.

A hardcoded list drifts. If the chart grows a sixth policy and nobody edits the
script, the inventory check silently stops covering it -- which is the same
shape of failure it was added to catch, one level up.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-network-policy.sh"


def declared() -> set[str]:
    """The `namespace/name` pairs the script's heredoc lists."""
    text = SCRIPT.read_text(encoding="utf-8")
    body = re.search(r"<<'POLICIES'\n(.*?)\nPOLICIES\n", text, re.S)
    assert body, "the expected_policies heredoc is not where this test looks"
    return {line for line in body.group(1).splitlines() if line.strip()}


def rendered() -> set[str]:
    output = subprocess.run(
        ["helm", "template", "sandbox", str(ROOT / "charts" / "sandbox")],
        capture_output=True, text=True, check=True,
    ).stdout
    import yaml

    return {
        f"{doc['metadata'].get('namespace')}/{doc['metadata']['name']}"
        for doc in yaml.safe_load_all(output)
        if doc and doc.get("kind") == "NetworkPolicy"
    }


class NetworkPolicyInventoryTests(unittest.TestCase):
    def test_the_script_lists_what_the_chart_renders(self) -> None:
        if not shutil.which("helm"):
            self.skipTest("helm is not installed")
        self.assertEqual(rendered(), declared())

    def test_the_list_is_not_empty(self) -> None:
        # An empty heredoc would make the check pass against any cluster,
        # including one with no policies at all.
        self.assertTrue(declared())


if __name__ == "__main__":
    unittest.main()
