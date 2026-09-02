"""Every ``control_plane.<name>`` a sibling module reaches for must actually exist.

The composition root is a plain module, so a call like ``control_plane.internal_token(...)``
resolves at call time, not at import time. Delete or rename the function and
nothing complains: the image builds, the module imports, the suite is green, and
the AttributeError waits on whichever request path happens to use it. That is
exactly how ``internal_token`` survived a refactor while three call sites kept
pointing at it.

The check is deliberately static and one-directional. It does not assert that
every name in the root is used -- an unused helper is untidy, a missing one is
an outage -- and it resolves names the way Python does at module level, so a
value assigned inside a function body does not count as a definition.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SANDBOX_CONTROL_PLANE_DIR = REPO_ROOT / "control_plane"
ROOT_MODULE = SANDBOX_CONTROL_PLANE_DIR / "core.py"


def module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Tuple):
                    names.update(
                        element.id
                        for element in target.elts
                        if isinstance(element, ast.Name)
                    )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.If):
            #Module-level conditionals still define names for the rest of the file.
            for inner in [*node.body, *node.orelse]:
                if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(inner.name)
                elif isinstance(inner, ast.Assign):
                    for target in inner.targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)
    return names


def attribute_uses(tree: ast.Module, root: str) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == root
        ):
            used.add(node.attr)
    return used


class ControlPlaneRootReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.defined = module_level_names(
            ast.parse(ROOT_MODULE.read_text(encoding="utf-8"))
        )

    def test_the_scan_finds_the_root_names(self) -> None:
        #Without this, a parser that returned an empty set would make the real
        #assertion below pass over nothing and report a guard that is not
        #looking at anything.
        self.assertIn("capability_ticket_for", self.defined)
        self.assertGreater(len(self.defined), 50)

    def test_no_sibling_reaches_for_a_name_the_root_does_not_define(self) -> None:
        missing: dict[str, set[str]] = {}
        for source in sorted(SANDBOX_CONTROL_PLANE_DIR.glob("*.py")):
            if source.name == "core.py":
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"))
            if "control_plane" not in module_level_names(tree):
                continue
            for name in sorted(attribute_uses(tree, "control_plane") - self.defined):
                missing.setdefault(name, set()).add(source.name)
        self.assertEqual(
            missing,
            {},
            "these names are reached for but not defined at the root; "
            "the call site raises AttributeError only when its path runs",
        )


if __name__ == "__main__":
    unittest.main()
