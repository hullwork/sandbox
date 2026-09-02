"""A repository-wide gate against names that are used but never bound.

``make test`` discovers ``tests/test_*.py`` and nothing else, so the seven
modules under ``scripts/`` and ``bench/`` -- the ones an operator runs by hand
against a real cluster -- are never imported by anything CI executes. A name
with no binder in one of those raises ``NameError`` at the moment it is needed,
which is the moment somebody is recovering a cluster. The same hole covers any
branch a ``skipUnless`` keeps from running.

The tree is clean today. This exists so it stays that way: the equivalent gate
in the agent repository, added at the same time, found three such names on
main, one of them in a browser E2E file that no workflow imports and one in a
PostgreSQL branch that skips without a database.

``symtable`` resolves each name against the scope that would bind it at run
time, so an import, a parameter, an assignment or a closure all count as bound
and only a genuinely free name is reported.

Two ways a gate like this goes quietly green are pinned below:

* scanning nothing (a glob that matches no file, a path that moved). The scan
  face is asserted to contain the specific directories that carry the risk and
  to be above a floor count.
* a checker that cannot fire. :class:`CheckerTests` feeds it sources that must
  and must not be reported, including the shapes that a naive implementation
  gets wrong: closures over an enclosing function's local, ``global``
  declarations, comprehension scopes, and module dunders.
"""
from __future__ import annotations

import builtins
import pathlib
import subprocess
import symtable
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Present in every module namespace at run time but never assigned in source,
#: plus the names the compiler synthesises itself. ``__conditional_annotations__``
#: and ``__annotate__`` come from PEP 649 deferred annotations and appear as free
#: module symbols under Python 3.14 but not under 3.11 -- listing them keeps the
#: gate's verdict identical across both versions in the CI matrix instead of red
#: on the newer one only.
MODULE_DUNDERS = frozenset({
    "__file__", "__name__", "__doc__", "__spec__", "__package__",
    "__builtins__", "__loader__", "__debug__", "__path__", "__all__",
    "__class__", "__annotations__", "__annotate__",
    "__conditional_annotations__",
})

#: A checked-out tree is scanned in full; the count only rules out "the glob
#: stopped matching". It is far below the real number so that deleting a module
#: does not make this fail for the wrong reason.
MINIMUM_SCANNED_FILES = 60


def tracked_python_files() -> list[pathlib.Path]:
    """Every git-tracked ``.py`` path.

    ``git ls-files`` rather than ``rglob`` on purpose: it will not walk into
    ``.venv`` or ``node_modules``, and it does not depend on the caller's
    working directory.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=120,
    ).stdout
    return [
        REPO_ROOT / name
        for name in listing.split("\0")
        if name and (REPO_ROOT / name).is_file()
    ]


def undefined_names(source: str, filename: str) -> list[tuple[str, str]]:
    """Return ``(scope, name)`` for every free name with nothing to bind it.

    A name is bound if any scope that could supply it assigns, imports or
    receives it as a parameter. ``symtable`` already resolved that, so a symbol
    still reported as free after the module namespace and the builtins are
    consulted has no binder anywhere.
    """
    top = symtable.symtable(source, filename, "exec")
    module_bound = {
        name for name in top.get_identifiers()
        if (symbol := top.lookup(name)).is_assigned()
        or symbol.is_imported()
        or symbol.is_parameter()
    }

    found: list[tuple[str, str]] = []

    def visit(table: symtable.SymbolTable, scope: str) -> None:
        for symbol in table.get_symbols():
            name = symbol.get_name()
            if not symbol.is_referenced():
                continue
            if (symbol.is_assigned() or symbol.is_imported()
                    or symbol.is_parameter()):
                continue
            #: ``is_free`` means an enclosing *function* scope binds it.
            if symbol.is_free():
                continue
            if name in module_bound or name in MODULE_DUNDERS:
                continue
            if hasattr(builtins, name):
                continue
            found.append((scope or "<module>", name))
        for child in table.get_children():
            child_scope = (
                f"{scope}.{child.get_name()}" if scope else child.get_name()
            )
            visit(child, child_scope)

    visit(top, "")
    return found


class RepositoryTests(unittest.TestCase):
    def test_no_tracked_module_uses_an_unbound_name(self) -> None:
        offences: list[str] = []
        for path in tracked_python_files():
            source = path.read_text(encoding="utf-8", errors="replace")
            relative = path.relative_to(REPO_ROOT)
            try:
                hits = undefined_names(source, str(relative))
            except SyntaxError as error:
                offences.append(f"{relative}: does not parse: {error}")
                continue
            for scope, name in sorted(set(hits)):
                offences.append(f"{relative}: {scope}: {name}")
        self.assertEqual(
            [], offences,
            "names used with nothing to bind them (NameError at run time):\n"
            + "\n".join(offences),
        )


class ScanFaceTests(unittest.TestCase):
    """The gate is worth exactly as much as the set of files it opens."""

    def setUp(self) -> None:
        self.files = tracked_python_files()

    def test_scan_reaches_the_directories_that_carry_the_risk(self) -> None:
        directories = {
            str(path.relative_to(REPO_ROOT).parent) for path in self.files
        }
        #: scripts/ and bench/ are the ones `make test` never imports.
        for required in ("scripts", "bench", "tests", "control_plane",
                         "runtime", "sandbox_platform"):
            self.assertIn(required, directories, f"{required} is not scanned")

    def test_scan_is_not_empty(self) -> None:
        self.assertGreaterEqual(
            len(self.files), MINIMUM_SCANNED_FILES,
            "the scan face collapsed; a green result would mean nothing",
        )

class CheckerTests(unittest.TestCase):
    """Both directions, because a matcher that never fires also reports zero."""

    def assert_reports(self, source: str, name: str) -> None:
        reported = {found for _, found in undefined_names(source, "<probe>")}
        self.assertIn(name, reported, f"{name!r} should have been reported")

    def assert_clean(self, source: str) -> None:
        self.assertEqual([], undefined_names(source, "<probe>"))

    def test_reports_a_module_level_free_name(self) -> None:
        self.assert_reports("import pathlib\nx = Path('.')\n", "Path")

    def test_reports_a_function_local_free_name(self) -> None:
        self.assert_reports("def f():\n    return admin_control\n",
                            "admin_control")

    def test_reports_a_name_only_used_inside_a_skipped_branch(self) -> None:
        self.assert_reports(
            "def f(flag):\n"
            "    if flag:\n"
            "        return validate_envelope(flag)\n"
            "    return None\n",
            "validate_envelope",
        )

    def test_accepts_imports_and_aliases(self) -> None:
        self.assert_clean("from pathlib import Path as P\nx = P('.')\n")

    def test_accepts_a_closure_over_an_enclosing_local(self) -> None:
        self.assert_clean(
            "def outer():\n"
            "    value = 1\n"
            "    def inner():\n"
            "        return value\n"
            "    return inner\n"
        )

    def test_accepts_a_module_global_written_from_a_function(self) -> None:
        self.assert_clean(
            "counter = 0\n"
            "def bump():\n"
            "    global counter\n"
            "    counter += 1\n"
        )

    def test_accepts_a_comprehension_target(self) -> None:
        self.assert_clean("values = [item for item in range(3)]\n")

    def test_accepts_module_dunders(self) -> None:
        self.assert_clean("import pathlib\nhere = pathlib.Path(__file__)\n")

    def test_accepts_a_class_attribute_referenced_through_self(self) -> None:
        self.assert_clean(
            "class C:\n"
            "    LIMIT = 1\n"
            "    def f(self):\n"
            "        return self.LIMIT\n"
        )


if __name__ == "__main__":
    unittest.main()
