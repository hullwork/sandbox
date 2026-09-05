from __future__ import annotations

import unittest

from sandbox_platform import mcp


class McpToolContractTests(unittest.TestCase):
    def test_checkpoint_description_matches_runtime_requirement(self) -> None:
        checkpoint = next(
            tool for tool in mcp.TOOLS if tool["name"] == "workspace_checkpoint"
        )
        description = checkpoint["description"]
        self.assertIn("create/restore requires an online Runtime", description)
        self.assertIn("list works offline", description)
        self.assertNotIn("list/restore works offline", description)

    def test_status_description_says_it_is_a_local_view(self) -> None:
        # SandboxManager.status() reads the in-process lease and never makes a
        # request. A description that reads like a health check would have the
        # host treat "control plane down" as "runtime not started".
        status = next(tool for tool in mcp.TOOLS if tool["name"] == "sandbox_status")
        description = status["description"]
        self.assertIn("does not contact the Control Plane", description)
        self.assertIn("cached lease", description)

    def test_status_reports_the_reported_runtime_class_not_a_constant(self) -> None:
        """The agent-facing status must not assert gVisor on a cluster without it.

        ``sandbox_status`` used to merge the literal ``"runtime": "gvisor"``
        into its result. A deployment that leaves ``SANDBOX_RUNTIME_CLASS``
        empty runs Runtimes on the cluster default runtime; the Control Plane
        reports ``cluster-default`` for exactly that case
        (``test_empty_runtime_class_reports_cluster_default_not_gvisor_isolation``)
        and the Console renders it, while this surface - the one an agent uses
        to reason about its own confinement - claimed isolation regardless.
        """
        recorded = {
            "session_id": "s", "workspace_id": "ws-000000000000",
            "workspace_ready": True, "sandbox_id": "sb-000000000000",
            "runtime_ready": True, "template": "default",
            "runtime_class": "cluster-default",
        }
        original = mcp.manager.status
        mcp.manager.status = lambda *args, **kwargs: dict(recorded)
        try:
            result = mcp.call_tool("sandbox_status", {})
        finally:
            mcp.manager.status = original
        payload = result["structuredContent"]
        self.assertEqual(payload["runtime_class"], "cluster-default")
        self.assertNotIn("gvisor", str(payload))


if __name__ == "__main__":
    unittest.main()
