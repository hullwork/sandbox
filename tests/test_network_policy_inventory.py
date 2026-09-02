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
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-network-policy.sh"
sys.path.insert(0, str(ROOT))
from control_plane.manifests import ManifestSettings, runtime_pod_manifest  # noqa: E402

# What sandbox-public-egress must and must not let a Runtime Pod reach. The
# list is the whole point of the policy, and until this test existed nothing
# read it back: 10.0.0.0/8 could be deleted and the suite stayed green.
PRIVATE_AND_RESERVED = {
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",  # CGNAT: EKS secondary CIDRs, ACK Terway, Tailscale
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "224.0.0.0/4",
    "240.0.0.0/4",
}
PUBLIC_PORTS = {("TCP", 80), ("TCP", 443)}
DNS_PORTS = {("UDP", 53), ("TCP", 53)}


def declared() -> set[str]:
    """The `namespace/name` pairs the script's heredoc lists."""
    text = SCRIPT.read_text(encoding="utf-8")
    body = re.search(r"<<'POLICIES'\n(.*?)\nPOLICIES\n", text, re.S)
    assert body, "the expected_policies heredoc is not where this test looks"
    return {line for line in body.group(1).splitlines() if line.strip()}


def helm_render() -> str:
    return subprocess.run(
        ["helm", "template", "sandbox", str(ROOT / "charts" / "sandbox")],
        capture_output=True, text=True, check=True,
    ).stdout


def kustomize_render() -> str:
    return subprocess.run(
        ["kubectl", "kustomize", str(ROOT / "k8s")],
        capture_output=True, text=True, check=True,
    ).stdout


def policies(output: str) -> dict[str, dict]:
    return {
        doc["metadata"]["name"]: doc
        for doc in yaml.safe_load_all(output)
        if doc and doc.get("kind") == "NetworkPolicy"
    }


def rendered() -> set[str]:
    return {
        f"{doc['metadata'].get('namespace')}/{doc['metadata']['name']}"
        for doc in policies(helm_render()).values()
    }


def ports(rule: dict) -> set[tuple[str, int]]:
    return {(item["protocol"], item["port"]) for item in rule["ports"]}


class NetworkPolicyInventoryTests(unittest.TestCase):
    def test_the_script_lists_what_the_chart_renders(self) -> None:
        if not shutil.which("helm"):
            self.skipTest("helm is not installed")
        self.assertEqual(rendered(), declared())

    def test_the_list_is_not_empty(self) -> None:
        # An empty heredoc would make the check pass against any cluster,
        # including one with no policies at all.
        self.assertTrue(declared())


class PublicEgressPolicyTests(unittest.TestCase):
    """The egress allow-list, read back from both render paths.

    The chart is hand-written, not generated from k8s/, and the checksum gate
    in test_helm_package only says "k8s/ changed, touch the chart" - it does
    not say the two agree. So every assertion here runs against the kustomize
    base and the Helm chart alike.
    """

    def renders(self) -> list[tuple[str, dict]]:
        found = []
        if shutil.which("kubectl"):
            found.append(("kustomize", policies(kustomize_render())["sandbox-public-egress"]))
        if shutil.which("helm"):
            found.append(("helm", policies(helm_render())["sandbox-public-egress"]))
        if not found:
            self.skipTest("neither kubectl nor helm is installed")
        return found

    def test_public_egress_excludes_every_private_and_reserved_range(self) -> None:
        for source, policy in self.renders():
            with self.subTest(render=source):
                self.assertEqual(policy["spec"]["policyTypes"], ["Egress"])
                rules = policy["spec"]["egress"]
                self.assertEqual(len(rules), 2, rules)
                (public,) = [rule for rule in rules if "ipBlock" in rule["to"][0]]
                (block,) = [peer["ipBlock"] for peer in public["to"]]
                self.assertEqual(block["cidr"], "0.0.0.0/0")
                self.assertEqual(set(block["except"]), PRIVATE_AND_RESERVED)
                self.assertEqual(len(block["except"]), len(PRIVATE_AND_RESERVED))
                self.assertEqual(ports(public), PUBLIC_PORTS)

    def test_dns_is_reachable_only_in_kube_system(self) -> None:
        for source, policy in self.renders():
            with self.subTest(render=source):
                (dns,) = [
                    rule for rule in policy["spec"]["egress"]
                    if "namespaceSelector" in rule["to"][0]
                ]
                self.assertEqual(ports(dns), DNS_PORTS)
                self.assertEqual(dns["to"], [{
                    "namespaceSelector": {"matchLabels": {
                        "kubernetes.io/metadata.name": "kube-system",
                    }},
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                }])

    def test_only_runtime_pods_are_granted_public_egress(self) -> None:
        # The selector is checked against the labels Control Plane really puts
        # on a Runtime Pod, not against a string copied from the manifest: a
        # label rename in manifests.py would otherwise cut the Runtime off
        # while this test kept passing on the old name.
        runtime = runtime_pod_manifest(
            ManifestSettings(
                workload_namespace="sandbox-workloads",
                workspace_pvc="workspaces",
                workspace_storage_mode="per-workspace",
                runtime_class="gvisor",
                runtime_node_selector={},
                runtime_tolerations=(),
                runtime_ttl_seconds=1800,
                runtime_hard_ttl_seconds=7200,
                runtime_name=lambda sandbox_id: f"sandbox-{sandbox_id}",
                template_image=lambda template_id, tenant_id: "sandbox-runtime:0.5.0",
                capability_key=lambda kind, subject: f"{kind}-key-{subject}",
                capability_epoch=lambda kind, subject: 7,
            ),
            "sb-0123456789ab",
            "ws-aaaaaaaaaaaa",
        )["metadata"]["labels"]
        for source, policy in self.renders():
            with self.subTest(render=source):
                selector = policy["spec"]["podSelector"]
                self.assertEqual(list(selector), ["matchLabels"], "an empty selector selects every Pod")
                self.assertTrue(selector["matchLabels"])
                for key, value in selector["matchLabels"].items():
                    self.assertEqual(runtime.get(key), value, key)
                # ...and it must not also fit the volume agent, which carries
                # the same label namespace with a different name.
                self.assertNotEqual(
                    selector["matchLabels"].get("app.kubernetes.io/name"), "sandbox-volume"
                )

    def test_base_and_chart_render_the_same_public_egress_spec(self) -> None:
        renders = dict(self.renders())
        if len(renders) < 2:
            self.skipTest("both kubectl and helm are needed to compare")
        self.assertEqual(renders["kustomize"]["spec"], renders["helm"]["spec"])


if __name__ == "__main__":
    unittest.main()
