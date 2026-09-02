"""The README's checkable claims, checked.

The "evaluate it in one minute" block is the first thing a newcomer reads and
the first thing they run, so a number in it that has drifted is worse than no
number: it is the reader's only calibration for whether ``make test`` did what
the README said it would. The count in that line was ``521`` while ``make
test`` collected 593 -- the claim had been stale long enough that nobody could
say when it stopped being true.

Only the test count is gated, and deliberately so. The wall-clock figure that
used to sit beside it is a property of the machine, not of the repository; a
gate cannot decide it and a reader on slower hardware cannot act on it, so it
is gone rather than asserted. What remains is a claim this repository owns and
can keep true in one edit -- the failure message below prints the replacement
line verbatim.

Discovery here is loader-only: modules are imported and cases counted, nothing
is executed twice.
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"

#: The line the "one minute" block tells a newcomer to run, and its claim.
CLAIM = re.compile(
    r"^make test\s+# (?P<count>\d+) unit and contract tests, "
    r"no network, no cluster$",
    re.M,
)

#: Rules out "discovery returned an empty suite", which would otherwise make
#: any equality assertion below a statement about two zeroes.
MINIMUM_COLLECTED = 400


def collect() -> list[unittest.TestCase]:
    """Every case ``make test`` would run, without running any of them."""
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(REPO_ROOT / "tests"),
        pattern="test_*.py",
        top_level_dir=str(REPO_ROOT),
    )
    cases: list[unittest.TestCase] = []

    def flatten(item) -> None:
        if isinstance(item, unittest.TestSuite):
            for child in item:
                flatten(child)
        else:
            cases.append(item)

    flatten(suite)
    return cases


class ReadmeTestCountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = README.read_text(encoding="utf-8")
        self.cases = collect()

    def test_discovery_actually_collected_a_suite(self) -> None:
        """A green equality between two empty sets is not a passing gate."""
        self.assertGreaterEqual(len(self.cases), MINIMUM_COLLECTED)

    def test_discovery_did_not_swallow_an_import_error(self) -> None:
        """``discover`` turns an unimportable module into a passing-looking case.

        It becomes a ``_FailedTest`` that only fails when *run*, so a module
        that stopped importing would still be counted here and the count would
        still agree with the README.
        """
        broken = [
            case.id() for case in self.cases
            if type(case).__name__ == "_FailedTest"
        ]
        self.assertEqual([], broken, f"modules that do not import: {broken}")

    def test_readme_states_the_count_make_test_collects(self) -> None:
        match = CLAIM.search(self.text)
        self.assertIsNotNone(
            match,
            "the `make test` line in README.md no longer has the shape this "
            "gate reads; update CLAIM and the line together",
        )
        claimed = int(match.group("count"))
        actual = len(self.cases)
        self.assertEqual(
            claimed, actual,
            "README.md line "
            f"{self.text[:match.start()].count(chr(10)) + 1} claims {claimed} "
            f"tests; `make test` collects {actual}. Replace it with:\n"
            f"make test                      # {actual} unit and contract "
            "tests, no network, no cluster",
        )


if __name__ == "__main__":
    unittest.main()
