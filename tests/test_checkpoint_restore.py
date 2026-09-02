"""Restore-path invariants of the workspace file service (file-service/file_service.py).

Archives are built in memory with ``tarfile`` and fed straight to
``ApiHandler.restore_checkpoint`` on a temporary workspace. Every rejected
archive must leave the workspace byte-for-byte unchanged and must not leave
staging directories behind; a valid archive must replace the tree while
keeping ``.sandbox`` intact.
"""
from __future__ import annotations

import importlib.util
import io
import os
import pathlib
import tarfile
import tempfile
import unittest


def load_file_service():
    os.environ.setdefault("FILE_SERVICE_CAPABILITY_KEY", "test-capability-key")
    os.environ.setdefault("WORKSPACE_ID", "ws-123456789abc")
    path = pathlib.Path(__file__).resolve().parents[1] / "file-service/file_service.py"
    spec = importlib.util.spec_from_file_location("restore_file_service", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


file_service = load_file_service()


def build_archive(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for info, payload in members:
            if payload is None:
                archive.addfile(info)
            else:
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
    buffer.seek(0)
    return buffer


def regular(name: str, payload: bytes = b"data\n") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.REGTYPE
    info.mode = 0o644
    return info, payload


def directory(name: str) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    return info, None


def special(name: str, kind: bytes, linkname: str = "") -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info, None


class RestoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.parent = pathlib.Path(self._temporary_directory.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        (self.root / ".sandbox").mkdir()
        (self.root / ".sandbox" / "last_used_at").write_text("1700000000")
        (self.root / "src").mkdir()
        (self.root / "src" / "keep.py").write_text("print('old')\n")
        (self.root / "notes.txt").write_text("old notes\n")
        self.original_workspace = file_service.WORKSPACE
        file_service.WORKSPACE = self.root
        self.before = self.snapshot()

    def tearDown(self) -> None:
        file_service.WORKSPACE = self.original_workspace
        self._temporary_directory.cleanup()

    def snapshot(self) -> dict[str, bytes | None]:
        """Every path under the workspace with file contents (None for dirs)."""
        entries: dict[str, bytes | None] = {}
        for path in sorted(self.root.rglob("*")):
            relative = str(path.relative_to(self.root))
            if path.is_symlink() or not path.is_file():
                entries[relative] = None
            else:
                entries[relative] = path.read_bytes()
        return entries

    def restore(self, buffer: io.BytesIO) -> dict[str, int]:
        handler = file_service._OperationCapture()
        return file_service.ApiHandler.restore_checkpoint(handler, buffer)

    def assert_rejected(self, members, message: str) -> None:
        with self.assertRaises(ValueError) as raised:
            self.restore(build_archive(members))
        self.assertIn(message, str(raised.exception))
        self.assertEqual(self.snapshot(), self.before)
        # Nothing may have been written outside the workspace either.
        self.assertEqual(sorted(p.name for p in self.parent.iterdir()), ["workspace"])


class RejectedArchiveTests(RestoreCase):
    def test_symlink_member_is_rejected(self) -> None:
        self.assert_rejected(
            [regular("ok.txt"), special("link", tarfile.SYMTYPE, "/etc/passwd")],
            "unsupported links or devices",
        )

    def test_hardlink_member_is_rejected(self) -> None:
        self.assert_rejected(
            [regular("ok.txt"), special("hard", tarfile.LNKTYPE, "ok.txt")],
            "unsupported links or devices",
        )

    def test_device_members_are_rejected(self) -> None:
        for kind in (tarfile.CHRTYPE, tarfile.BLKTYPE):
            with self.subTest(kind=kind):
                self.assert_rejected(
                    [special("dev", kind)], "unsupported links or devices"
                )

    def test_fifo_member_is_rejected(self) -> None:
        self.assert_rejected([special("pipe", tarfile.FIFOTYPE)], "unsupported links or devices")

    def test_absolute_path_is_rejected(self) -> None:
        self.assert_rejected(
            [regular("/w4-restore-escape/evil.txt")], "unsafe path"
        )

    def test_parent_traversal_is_rejected(self) -> None:
        for name in ("../escaped.txt", "src/../../escaped.txt", "src/../ok.txt"):
            with self.subTest(name=name):
                self.assert_rejected([regular(name)], "unsafe path")
        self.assertFalse((self.parent / "escaped.txt").exists())
        self.assertFalse((self.root / ".sandbox" / "escaped.txt").exists())

    def test_empty_and_dot_names_are_rejected(self) -> None:
        for name in ("", ".", "./"):
            with self.subTest(name=name):
                self.assert_rejected([regular(name)], "unsafe path")

    def test_sandbox_metadata_members_are_rejected(self) -> None:
        for name in (".sandbox", ".sandbox/last_used_at", ".sandbox/restore-x/y"):
            with self.subTest(name=name):
                self.assert_rejected([regular(name)], "reserved path")
        self.assertEqual(
            (self.root / ".sandbox" / "last_used_at").read_text(), "1700000000"
        )

    def test_duplicate_paths_are_rejected(self) -> None:
        self.assert_rejected(
            [regular("dup.txt", b"one"), regular("./dup.txt", b"two")],
            "duplicate paths",
        )

    def test_too_many_entries_is_rejected(self) -> None:
        original = file_service.MAX_CHECKPOINT_ENTRIES
        file_service.MAX_CHECKPOINT_ENTRIES = 3
        try:
            self.assert_rejected(
                [regular(f"f{i}.txt") for i in range(4)], "too many entries"
            )
            # Exactly the limit is still accepted.
            result = self.restore(build_archive([regular(f"f{i}.txt") for i in range(3)]))
            self.assertEqual(result["files"], 3)
        finally:
            file_service.MAX_CHECKPOINT_ENTRIES = original

    def test_expanded_size_over_limit_is_rejected(self) -> None:
        original = file_service.MAX_CHECKPOINT_SOURCE_BYTES
        file_service.MAX_CHECKPOINT_SOURCE_BYTES = 10
        try:
            self.assert_rejected(
                [regular("a.bin", b"x" * 6), regular("b.bin", b"y" * 5)],
                "beyond size limit",
            )
            result = self.restore(
                build_archive([regular("a.bin", b"x" * 6), regular("b.bin", b"y" * 4)])
            )
            self.assertEqual(result, {"files": 2, "bytes": 10})
        finally:
            file_service.MAX_CHECKPOINT_SOURCE_BYTES = original

    def test_rejection_happens_before_any_staging_directory_exists(self) -> None:
        self.assert_rejected(
            [regular("ok.txt"), special("link", tarfile.SYMTYPE, "ok.txt")],
            "unsupported",
        )
        leftovers = [
            p.name for p in (self.root / ".sandbox").iterdir()
            if p.name.startswith(("restore-", "old-"))
        ]
        self.assertEqual(leftovers, [])


class AcceptedArchiveTests(RestoreCase):
    def test_valid_archive_replaces_the_tree_and_keeps_metadata(self) -> None:
        result = self.restore(build_archive([
            directory("src"),
            directory("src/pkg"),
            regular("src/pkg/__init__.py", b""),
            regular("src/new.py", b"print('new')\n"),
            regular("README.md", b"# restored\n"),
            directory("empty"),
        ]))
        self.assertEqual(result, {"files": 3, "bytes": len(b"print('new')\n") + len(b"# restored\n")})
        after = self.snapshot()
        self.assertEqual(after["src/new.py"], b"print('new')\n")
        self.assertEqual(after["README.md"], b"# restored\n")
        self.assertEqual(after["src/pkg/__init__.py"], b"")
        self.assertIsNone(after["empty"])
        # The old tree is gone entirely, not merged.
        self.assertNotIn("notes.txt", after)
        self.assertNotIn("src/keep.py", after)
        # Lifecycle metadata survives and no staging directory is left behind.
        self.assertEqual(after[".sandbox/last_used_at"], b"1700000000")
        leftovers = [
            p.name for p in (self.root / ".sandbox").iterdir()
            if p.name.startswith(("restore-", "old-"))
        ]
        self.assertEqual(leftovers, [])

    def test_restore_counts_only_regular_files(self) -> None:
        result = self.restore(build_archive([directory("only-dirs"), directory("only-dirs/child")]))
        self.assertEqual(result, {"files": 0, "bytes": 0})
        self.assertTrue((self.root / "only-dirs" / "child").is_dir())
        self.assertFalse((self.root / "notes.txt").exists())

    def test_failed_swap_restores_the_old_tree(self) -> None:
        """If installing the staged snapshot blows up, the old tree comes back."""
        original = file_service.ApiHandler._swap_in_restored_tree

        def exploding_swap(staging, retired):
            # Retire the old tree, then fail before installing anything.
            file_service._write_restore_journal("retiring", staging, retired)
            for child in list(self.root.iterdir()):
                if child.name != ".sandbox":
                    os.replace(child, retired / child.name)
            try:
                raise OSError("simulated ENOSPC during install")
            finally:
                for child in list(retired.iterdir()):
                    os.replace(child, self.root / child.name)
                file_service._clear_restore_journal()

        file_service.ApiHandler._swap_in_restored_tree = staticmethod(exploding_swap)
        try:
            with self.assertRaises(OSError):
                self.restore(build_archive([regular("new.txt")]))
        finally:
            file_service.ApiHandler._swap_in_restored_tree = staticmethod(original)
        self.assertEqual(self.snapshot(), self.before)


if __name__ == "__main__":
    unittest.main()
