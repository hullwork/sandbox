"""Documentation may claim a provider selector only once one exists.

`README.md` states the rule the project holds itself to: "There is no provider
plug-in surface advertised that does not exist." `docs/ARCHITECTURE.md` broke it
by saying the Runtime provider "is selected independently by
SANDBOX_RUNTIME_DRIVER" while `configured_runtime_driver()` constructs one
driver and never reads that variable - the variable's only effect is an equality
check that refuses every other value.

The check is conditional on purpose. It reads the selector's presence out of the
source and flips: while there is no selector the prose must not promise one, and
the day a registry lands the same test starts requiring the prose to say so.
"""

from __future__ import annotations

import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SELECTOR_VARIABLE = "SANDBOX_RUNTIME_DRIVER"


def selector_exists() -> bool:
    """Whether ``configured_runtime_driver`` actually dispatches on the name."""
    source = (ROOT / "control_plane/core.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "configured_runtime_driver"
    )
    return any(
        isinstance(node, ast.Name) and node.id == SELECTOR_VARIABLE
        for node in ast.walk(function)
    )


class RuntimeProviderClaimTests(unittest.TestCase):
    def test_the_startup_check_still_refuses_every_other_name(self) -> None:
        # The premise of the whole file: the variable is a gate, not a switch.
        source = (ROOT / "control_plane/core.py").read_text(encoding="utf-8")
        self.assertIn(f'if {SELECTOR_VARIABLE} != "gvisor":', source)

    def test_architecture_does_not_promise_a_selector_that_is_absent(self) -> None:
        architecture = (ROOT / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        claim = f"provider is selected independently by `{SELECTOR_VARIABLE}`"
        if selector_exists():
            self.assertIn(
                claim,
                architecture,
                "a provider selector now exists; docs/ARCHITECTURE.md should say so",
            )
        else:
            self.assertNotIn(
                claim,
                architecture,
                "docs/ARCHITECTURE.md advertises a provider selector that "
                "configured_runtime_driver() does not implement",
            )

    def test_the_readme_still_states_the_rule_this_enforces(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "There is no provider plug-in\n  surface advertised that does not exist.",
            readme,
        )


if __name__ == "__main__":
    unittest.main()
