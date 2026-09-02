"""Retention GC for inactive Workspace directories (file-service/gc_workspaces.py).

No test in this suite called anything in that module: ``last_used_at``,
``collect_expired`` and ``purge_workspace`` all scored zero, and the only hit
on ``main`` came from ``unittest.main()`` in fifty unrelated files.

Two properties carry the weight here.

The first is that ``.sandbox/last_used_at`` is **untrusted input**. It sits on a
filesystem the tenant writes directly -- the sandbox shell and file-service
share the subPath and the uid -- so a bare ``> 0`` check means one
``echo 9999999999 > .sandbox/last_used_at`` buys permanent exemption from TTL
collection. That is a cost-side hole with no error anywhere: the only symptom
is that the volume quietly stops shrinking. The marker's own mtime is the
fallback, and it is capped as well, because the same hand can push it forward
with ``touch -d``.

The second is that one undeletable directory must not cancel the rest of the
sweep. Without per-workspace isolation the amount actually collected depends on
where the first bad directory sorts, and it fails in the same place, in the
same order, every hour.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_gc():
    """Load by file path: the module lives outside any importable package."""
    path = ROOT / "file-service/gc_workspaces.py"
    spec = importlib.util.spec_from_file_location("workspace_gc_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workspace_gc = load_gc()


class CollectionTests(unittest.TestCase):
    def test_only_expired_and_well_named_directories_are_collected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="w4-gc-") as root_dir:
            root = pathlib.Path(root_dir)
            expired = root / "ws-111111111111"
            recent = root / "ws-222222222222"
            unrelated = root / "not-a-workspace"
            for path, timestamp in ((expired, 100), (recent, 950), (unrelated, 1)):
                (path / ".sandbox").mkdir(parents=True)
                (path / ".sandbox/last_used_at").write_text(str(timestamp))
            self.assertEqual(
                workspace_gc.collect_expired(root, now=1000, ttl_seconds=100),
                [expired],
            )

    def test_a_symlinked_workspace_is_not_a_candidate(self) -> None:
        # A link named like a workspace would otherwise put whatever it points
        # at on the deletion list.
        with tempfile.TemporaryDirectory(prefix="w4-gc-") as root_dir, \
                tempfile.TemporaryDirectory(prefix="w4-gc-out-") as outside_dir:
            root = pathlib.Path(root_dir)
            outside = pathlib.Path(outside_dir)
            (outside / "keep.txt").write_text("keep")
            (root / "ws-333333333333").symlink_to(outside, target_is_directory=True)
            # `now` has to sit past the target's real mtime, or the link is
            # excluded for being fresh rather than for being a link and the
            # assertion holds no matter what the guard does.
            future = int(outside.stat().st_mtime) + 10_000
            self.assertEqual(
                workspace_gc.collect_expired(root, now=future, ttl_seconds=1), []
            )

    def test_a_non_positive_ttl_is_refused(self) -> None:
        # ttl_seconds <= 0 would make every workspace expired at once.
        with tempfile.TemporaryDirectory(prefix="w4-gc-") as root_dir:
            with self.assertRaises(ValueError):
                workspace_gc.collect_expired(
                    pathlib.Path(root_dir), now=1000, ttl_seconds=0
                )


class PurgeTests(unittest.TestCase):
    def test_purge_removes_the_workspace_without_following_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="w4-gc-") as root_dir, \
                tempfile.TemporaryDirectory(prefix="w4-gc-out-") as outside_dir:
            root = pathlib.Path(root_dir).resolve()
            outside = pathlib.Path(outside_dir).resolve()
            (outside / "keep.txt").write_text("keep")
            workspace = root / "ws-111111111111"
            workspace.mkdir()
            (workspace / "data.txt").write_text("delete")
            (workspace / "outside").symlink_to(outside, target_is_directory=True)

            result = workspace_gc.purge_workspace(root, workspace)

            self.assertTrue(result["deleted"])
            self.assertFalse(workspace.exists())
            # A tenant that puts `ln -s / mnt` in its own workspace must not be
            # able to have the GC delete the host through it.
            self.assertEqual((outside / "keep.txt").read_text(), "keep")

    def test_purge_refuses_a_path_outside_the_configured_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="w4-gc-") as root_dir, \
                tempfile.TemporaryDirectory(prefix="w4-gc-out-") as outside_dir:
            root = pathlib.Path(root_dir).resolve()
            stranger = pathlib.Path(outside_dir).resolve() / "ws-111111111111"
            stranger.mkdir()
            with self.assertRaises(ValueError):
                workspace_gc.purge_workspace(root, stranger)
            self.assertTrue(stranger.exists())


class LastUsedAtTrustTests(unittest.TestCase):
    """The marker is tenant-writable, so it needs a ceiling where it is read."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="w4-gc-marker-")
        self.addCleanup(self.tempdir.cleanup)
        self.root = pathlib.Path(self.tempdir.name)
        self.workspace = self.root / "ws-111111111111"
        (self.workspace / ".sandbox").mkdir(parents=True)
        self.marker = self.workspace / ".sandbox/last_used_at"

    def write_marker(self, text: str, *, mtime: int) -> None:
        self.marker.write_text(text)
        os.utime(self.marker, (mtime, mtime))

    def test_a_value_within_the_bound_is_taken_as_written(self) -> None:
        # The marker is the book of record in the normal case, even when its
        # own mtime looks older.
        self.write_marker("500", mtime=1)
        self.assertEqual(workspace_gc.last_used_at(self.workspace, now=1000), 500)

    def test_a_slightly_future_value_survives_clock_skew(self) -> None:
        # The writer and the GC are different nodes. Inside the tolerance this
        # is skew, not forgery, and rolling it back would collect live data.
        self.write_marker("4600", mtime=1)
        self.assertEqual(workspace_gc.last_used_at(self.workspace, now=1000), 4600)

    def test_a_far_future_value_falls_back_to_the_marker_mtime(self) -> None:
        self.write_marker(str(1000 + workspace_gc.LAST_USED_MAX_AHEAD_SECONDS + 1), mtime=300)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            used = workspace_gc.last_used_at(self.workspace, now=1000)
        self.assertEqual(used, 300)
        # One log line is the only trace this leaves apart from "the collected
        # volume quietly went down".
        self.assertIn(self.workspace.name, stderr.getvalue())

    def test_a_forged_marker_mtime_is_capped_as_well(self) -> None:
        # The value and the mtime are written by the same hand: `touch -d
        # 2030-01-01` moves the fallback too. Capping it takes the payoff for
        # forging from "exempt forever" down to "last real contact + TTL + 24h".
        self.write_marker("9999999999", mtime=1900000000)
        with contextlib.redirect_stderr(io.StringIO()):
            used = workspace_gc.last_used_at(self.workspace, now=1000)
        self.assertEqual(used, 1000 + workspace_gc.LAST_USED_MAX_AHEAD_SECONDS)

    def test_an_unparseable_value_still_falls_back_to_the_directory_mtime(self) -> None:
        self.write_marker("not-a-number", mtime=300)
        os.utime(self.workspace, (777, 777))
        self.assertEqual(workspace_gc.last_used_at(self.workspace, now=1000), 777)

    def test_a_forged_marker_does_not_escape_ttl_end_to_end(self) -> None:
        """The whole point, asserted through collect_expired rather than the helper."""
        self.write_marker("9999999999", mtime=100)
        os.utime(self.workspace, (100, 100))
        with contextlib.redirect_stderr(io.StringIO()):
            expired = workspace_gc.collect_expired(self.root, now=1000, ttl_seconds=100)
        self.assertEqual(expired, [self.workspace])


class SweepIsolationTests(unittest.TestCase):
    """One bad directory must not decide how much the sweep collects."""

    def run_main(self, root: pathlib.Path) -> tuple[int, dict]:
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, {
            "WORKSPACE_GC_ROOT": str(root),
            "WORKSPACE_DATA_TTL_SECONDS": "1",
            "WORKSPACE_GC_DRY_RUN": "false",
        }), contextlib.redirect_stdout(buffer):
            code = workspace_gc.main()
        return code, json.loads(buffer.getvalue())

    def make_expired(self, root: pathlib.Path, name: str) -> pathlib.Path:
        workspace = root / name
        (workspace / ".sandbox").mkdir(parents=True)
        (workspace / ".sandbox/last_used_at").write_text("1")
        return workspace

    def test_one_undeletable_workspace_does_not_skip_the_rest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="w4-gc-") as root_dir:
            root = pathlib.Path(root_dir).resolve()
            broken = self.make_expired(root, "ws-aaaaaaaaaaaa")
            healthy = self.make_expired(root, "ws-bbbbbbbbbbbb")
            real_rmtree = workspace_gc.shutil.rmtree

            def rmtree(path, *args, **kwargs):
                if pathlib.Path(path).name == broken.name:
                    raise OSError(13, "Permission denied")
                return real_rmtree(path, *args, **kwargs)

            with mock.patch.object(workspace_gc.shutil, "rmtree", rmtree):
                code, report = self.run_main(root)

            # broken sorts first, so without isolation healthy survives.
            self.assertFalse(healthy.exists(), "the broken one must not block the rest")
            self.assertTrue(broken.exists())
            self.assertEqual(report["workspaces"], [healthy.name])
            self.assertEqual(
                [entry["workspace_id"] for entry in report["failed"]], [broken.name]
            )
            self.assertEqual(report["status"], "partial")
            # Besides the log, the exit code is the CronJob's only signal.
            self.assertEqual(code, 1)

    def test_a_clean_run_reports_ok_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory(prefix="w4-gc-") as root_dir:
            root = pathlib.Path(root_dir).resolve()
            workspace = self.make_expired(root, "ws-cccccccccccc")
            code, report = self.run_main(root)
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["failed"], [])
            self.assertEqual(report["workspaces"], [workspace.name])
            self.assertFalse(workspace.exists())

    def test_a_dry_run_reports_candidates_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory(prefix="w4-gc-") as root_dir:
            root = pathlib.Path(root_dir).resolve()
            workspace = self.make_expired(root, "ws-dddddddddddd")
            buffer = io.StringIO()
            with mock.patch.dict(os.environ, {
                "WORKSPACE_GC_ROOT": str(root),
                "WORKSPACE_DATA_TTL_SECONDS": "1",
                "WORKSPACE_GC_DRY_RUN": "true",
            }), contextlib.redirect_stdout(buffer):
                code = workspace_gc.main()
            report = json.loads(buffer.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(report["dry_run"])
            self.assertEqual(report["candidates"], 1)
            self.assertTrue(workspace.exists(), "a dry run must not delete anything")


if __name__ == "__main__":
    unittest.main()
