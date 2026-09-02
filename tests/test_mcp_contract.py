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


if __name__ == "__main__":
    unittest.main()
