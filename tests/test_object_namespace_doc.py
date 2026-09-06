"""The object path vocabulary a caller must obey has to be written down.

Every prefix rule below lives in ``control_plane/core.py`` and nowhere else: an
integrator discovers them by sending requests and reading 400s. The errors name
the allowed set, which makes the API usable by trial; the documentation is what
makes it usable by reading. This test keeps the two from drifting apart, in the
direction that matters - a set widened or narrowed in code without the table
following.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def allowed_roots_in_source() -> list[set[str]]:
    """Every ``allowed_roots={...}`` literal passed inside ``object_location``."""
    source = (ROOT / "control_plane/core.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "object_location"
    )
    found = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "allowed_roots" and isinstance(keyword.value, ast.Set):
                found.append({element.value for element in keyword.value.elts})
    return found


class ObjectNamespaceDocumentationTests(unittest.TestCase):
    def setUp(self) -> None:
        document = (ROOT / "docs/API.md").read_text(encoding="utf-8")
        start = document.index("### Object namespace")
        self.section = document[start:document.index("\n### ", start + 1)]

    def test_both_scopes_declare_the_roots_the_code_enforces(self) -> None:
        found = allowed_roots_in_source()
        self.assertEqual(
            [
                {"source", "derived", "meta"},
                {"inputs", "outputs", "artifacts", "logs", "meta"},
            ],
            found,
            "object_location's allowed roots changed; update docs/API.md with them",
        )
        for roots in found:
            for root in roots:
                self.assertIn(
                    f"`{root}/`",
                    self.section,
                    f"docs/API.md does not list the {root!r} object root",
                )

    def test_the_workspace_transfer_directions_are_stated(self) -> None:
        api = (ROOT / "control_plane/api.py").read_text(encoding="utf-8")
        destination = re.search(r'destination\.startswith\("([^"]+)"\)', api)
        self.assertIsNotNone(destination, "the import destination guard moved")
        self.assertIn(destination.group(1), self.section)
        # Export is the other direction and names a different root; a table that
        # only mentioned one of them would read as if they were interchangeable.
        self.assertIn("`artifacts/`", self.section)
        self.assertIn("objects/export", self.section)
        self.assertIn("objects/import", self.section)


if __name__ == "__main__":
    unittest.main()
