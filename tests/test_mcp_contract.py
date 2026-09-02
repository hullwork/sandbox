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


if __name__ == "__main__":
    unittest.main()
