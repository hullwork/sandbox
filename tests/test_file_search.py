"""The File Service search surface (file-service/file_service.py, ``handle_glob``).

``handle_glob``, ``handle_grep`` and ``handle_read`` had no caller anywhere in
this suite. The half migrated here is the one with a security boundary: a glob
pattern is caller-controlled, and matching walks the tree rather than resolving
one path, so both a traversal in the *pattern* and a symlink whose target sits
outside the workspace have to be stopped by this function -- ``safe_path``
never sees either.

The neighbouring semantics come with it because they are what makes those two
assertions mean something: if ``*`` silently crossed directories or pruning
skipped the wrong subtree, an escaping match could disappear for the wrong
reason and the test would still pass.

No HTTP: ``ApiHandler`` is built directly with ``send_json`` replaced, the same
shape ``test_checkpoint_restore.py`` uses. ``WORKSPACE`` is a module constant,
pointed at a temporary directory for the duration.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_file_service():
    os.environ.setdefault("FILE_SERVICE_TOKEN", "test-token")
    os.environ.setdefault("WORKSPACE_ID", "ws-123456789abc")
    # The image fails closed without a capability key: a process that started
    # without one would serve every request unverified and look healthy doing
    # it. This loader executes the module source, so it has to supply one.
    os.environ.setdefault("FILE_SERVICE_CAPABILITY_KEY", "test-capability-key")
    # file_service.py does `from workspace_contract import ...`, which lives at
    # the repository root and is copied next to it inside the image.
    sys.path.insert(0, str(ROOT))
    path = ROOT / "file-service/file_service.py"
    spec = importlib.util.spec_from_file_location("search_file_service", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


file_service = load_file_service()


class HandlerCase(unittest.TestCase):
    def setUp(self) -> None:
        previous = file_service.WORKSPACE
        self.addCleanup(setattr, file_service, "WORKSPACE", previous)
        self.tempdir = tempfile.TemporaryDirectory(prefix="w4-search-")
        self.addCleanup(self.tempdir.cleanup)
        self.root = pathlib.Path(self.tempdir.name).resolve()
        file_service.WORKSPACE = self.root
        self.handler = object.__new__(file_service.ApiHandler)
        self.captured: dict = {}
        self.handler.send_json = lambda status, payload: self.captured.update(
            status=status, payload=payload
        )

    def write(self, name: str, content: str) -> pathlib.Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    @property
    def payload(self) -> dict:
        return self.captured["payload"]


class GlobEscapeTests(HandlerCase):
    """The two cases ``safe_path`` cannot reach."""

    def test_a_traversal_in_the_pattern_is_rejected(self) -> None:
        # The pattern is caller-controlled and is not a path argument, so it
        # never passes through the resolver that guards reads and writes.
        for bad in ("../*.py", "/etc/*"):
            with self.subTest(pattern=bad):
                with self.assertRaises(ValueError):
                    self.handler.handle_glob("", bad, 0)

    def test_a_symlink_pointing_outside_the_workspace_is_dropped(self) -> None:
        """A match here would hand the caller a file it may not read.

        The link's own name is legal and inside the workspace; only its target
        is outside, so the check has to be on the resolved location.
        """
        outside = pathlib.Path(self.tempdir.name).parent / "w4-escape-target.py"
        outside.write_text("secret", encoding="utf-8")
        self.addCleanup(outside.unlink)
        (self.root / "link.py").symlink_to(outside)
        self.handler.handle_glob("", "*.py", 0)
        self.assertEqual(self.payload["matches"], [])

    def test_a_symlink_pointing_inside_the_workspace_is_not_dropped(self) -> None:
        """The reverse guard: "drop everything" would pass the test above too.

        Two observed details this pins rather than wishes away. The walk yields
        the *resolved* path, so a link inside the workspace is reported under
        its target's name and is unreachable by its own; and because the target
        is also walked directly, one file comes back twice with ``total`` 2.
        Whether that duplicate is worth changing is a product question -- what
        matters here is that the escape check drops escapes only, so the case
        above cannot pass by accident.
        """
        self.write("src/real.py", "x")
        (self.root / "alias.py").symlink_to(self.root / "src/real.py")
        self.handler.handle_glob("", "*.py", 0)
        self.assertEqual(self.payload["matches"], ["src/real.py", "src/real.py"])
        self.assertEqual(self.payload["total"], 2)
        self.handler.handle_glob("", "alias.py", 0)
        self.assertEqual(self.payload["matches"], [])


class GlobSemanticsTests(HandlerCase):
    """What "found" and "not found" mean, so an escape cannot hide among them."""

    def test_a_bare_pattern_matches_at_any_depth_newest_first(self) -> None:
        old = self.write("src/old.py", "x")
        deep = self.write("src/a/b/deep.py", "x")
        self.write("notes.md", "x")
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(deep, (time.time(), time.time()))
        self.handler.handle_glob("", "*.py", 0)
        self.assertEqual(self.payload["matches"], ["src/a/b/deep.py", "src/old.py"])

    def test_a_single_star_does_not_cross_directories(self) -> None:
        self.write("src/top.py", "x")
        self.write("src/a/nested.py", "x")
        self.handler.handle_glob("", "src/*.py", 0)
        self.assertEqual(self.payload["matches"], ["src/top.py"])

    def test_a_double_star_crosses_directories(self) -> None:
        self.write("src/top.py", "x")
        self.write("src/a/b/nested.py", "x")
        self.handler.handle_glob("", "src/**/*.py", 0)
        self.assertEqual(
            sorted(self.payload["matches"]), ["src/a/b/nested.py", "src/top.py"]
        )

    def test_a_search_root_scopes_the_match(self) -> None:
        self.write("src/in.py", "x")
        self.write("other/out.py", "x")
        self.handler.handle_glob("src", "*.py", 0)
        self.assertEqual(self.payload["matches"], ["src/in.py"])

    def test_noise_directories_are_pruned(self) -> None:
        self.write("node_modules/pkg/index.js", "x")
        self.write("app.js", "x")
        self.handler.handle_glob("", "*.js", 0)
        self.assertEqual(self.payload["matches"], ["app.js"])

    def test_the_compression_mirror_is_pruned_but_a_user_directory_is_not(self) -> None:
        """Pruning is by path, not by the name "compressed".

        The mirror holds a second copy of content that is already in the
        workspace, and a model that finds both concludes there are two
        implementations. A directory the user happens to call ``compressed``
        is still their own and stays searchable.
        """
        # The mirror file is given the extension the glob is looking for on
        # purpose: with a .txt copy the pattern could not match it either way,
        # and the "mirror is pruned" half of this assertion would hold no
        # matter what pruning did.
        self.write("src/handler.py", "def handle_request(): ...")
        self.write("artifacts/compressed/handler.py", "def handle_request(): ...")
        self.write("compressed/mine.py", "def handle_request(): ...")
        self.handler.handle_glob("", "*.py", 0)
        self.assertEqual(
            sorted(self.payload["matches"]), ["compressed/mine.py", "src/handler.py"]
        )

    def test_dot_directories_are_searchable_unless_they_are_named(self) -> None:
        """Leading dot is not the criterion; the list is.

        What a blanket rule buys is "the model cannot find .github/workflows,
        so it reports the file does not exist". Pruning only applies to glob
        and grep and never blocks an explicit read, so hiding these has no
        security value to trade for that.
        """
        self.write(".github/workflows/ci.yml", "on: push")
        self.write(".git/objects/pack/keep-out.yml", "binary")
        self.handler.handle_glob("", "*.yml", 0)
        self.assertEqual(self.payload["matches"], [".github/workflows/ci.yml"])


if __name__ == "__main__":
    unittest.main()
