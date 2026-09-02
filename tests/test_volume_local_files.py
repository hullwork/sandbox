"""Workspace access through the mounted volume (control_plane/volume.py).

The same ``/v1/workspaces/{id}/files/*`` request has two implementations: the
Runtime forwards it to its file-service sidecar when one exists, and the volume
role reads the mounted volume directly when one does not. A caller cannot tell
which answered, so the two must agree, and the local one must sanitise paths on
its own -- there is no sidecar in front of it to do that.

Nothing in ``volume.py`` was called by any test in this suite: all fifteen of
its module-level names scored zero. The security half of that gap is what most
of this module is: ``..``, absolute paths, the reserved ``.sandbox`` directory,
NUL bytes, a workspace id that is itself a traversal, and a **symbolic link
that escapes after resolution** -- the one a string check cannot see.

The remaining half is window semantics, because a read window that disagrees
with file-service returns different bytes for the same file depending on
whether a Runtime happens to be up.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import workspace_contract  # noqa: E402


def load_volume_role():
    """Import the real core and volume modules in the volume role.

    ``core.py`` reads its configuration at import, which is why every other
    module in this suite loads it in a subprocess. This one needs the functions
    themselves, so it imports them in-process and puts ``os.environ`` back
    immediately: a leaked ``SANDBOX_CONTROL_PLANE_ROLE`` would silently
    reconfigure the subprocess probes other modules spawn later.

    The volume role is not a way around Kubernetes. It is the role that serves
    these routes, and it is defined as the one holding no Kubernetes client and
    no ``SIGNING_KEY`` -- it runs in the untrusted workload namespace, so
    directory isolation is the only thing standing between workspaces.

    The import is then unwound from ``sys.modules`` and from the package
    object, keeping the module objects alive only through the names returned
    here. That is not tidiness. ``reaper.py`` says ``from . import core as
    control_plane``, and ``from package import submodule`` reads the parent
    package's attribute first and only falls back to ``sys.modules`` when that
    attribute is absent. ``test_reaper_behavior`` substitutes a fake core by
    writing ``sys.modules["control_plane.core"]``, which works only while
    nothing has ever imported the real core in-process and left the attribute
    bound. Leaving it bound here made the fake invisible and eleven of that
    module's tests reached a real driver with no Kubernetes client.
    """
    package = importlib.import_module("control_plane")
    preloaded = {name for name in sys.modules if name.startswith("control_plane")}
    required = {
        "SANDBOX_CONTROL_PLANE_ROLE": "volume",
        "VOLUME_AGENT_TOKEN": "test-volume-agent-token",
        "SANDBOX_CONTROL_PLANE_TOKEN": "test-control-plane-token",
        "OBJECT_STORE_ENDPOINT": "http://127.0.0.1:9000",
        "OBJECT_STORE_ACCESS_KEY": "test-access",
        "OBJECT_STORE_SECRET_KEY": "test-secret",
        "WORKSPACE_ID_KEY": "test-workspace-id-key",
    }
    previous = {name: os.environ.get(name) for name in required}
    os.environ.update(required)
    try:
        from control_plane import core, volume
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        for name in [n for n in sys.modules if n.startswith("control_plane")]:
            if name in preloaded:
                continue
            del sys.modules[name]
            attribute = name.rpartition(".")[2]
            if getattr(package, attribute, None) is not None:
                delattr(package, attribute)
    # If some other module imported core first, the import above returned that
    # instance and the environment set here did nothing. Say so loudly instead
    # of testing a differently configured module in silence.
    assert core.CONTROL_PLANE_ROLE == "volume", (
        f"control_plane.core was already imported as role "
        f"{core.CONTROL_PLANE_ROLE!r}; this module would be testing that "
        "configuration instead of the volume role"
    )
    assert core.KUBE is None, "the volume role must hold no Kubernetes client"
    # The unwind above has to be complete, or the next module to load the real
    # core gets this configuration without asking for it.
    assert "control_plane.core" not in sys.modules, "core was left loaded"
    assert not hasattr(package, "core"), "the package still points at this core"
    return core, volume


core, volume = load_volume_role()

WORKSPACE = "ws-0123456789ab"


class WindowContractTests(unittest.TestCase):
    """One authority for the read window, on the volume side too.

    ``test_standalone_contract`` pins the numbers in ``workspace_contract`` and
    checks that each image copies that file. Neither of those notices
    ``core.py`` drifting back to a literal of its own, which is exactly how the
    two implementations would start answering the same read differently.
    """

    def test_local_limits_come_from_the_shared_contract(self) -> None:
        for contract_name, local_name in (
            ("MAX_LIST_ENTRIES", "LOCAL_MAX_LIST_ENTRIES"),
            ("MAX_READ_SOURCE_BYTES", "LOCAL_MAX_READ_SOURCE_BYTES"),
            ("MAX_READ_LINES", "LOCAL_MAX_READ_LINES"),
            ("MAX_READ_CHARS", "LOCAL_MAX_READ_CHARS"),
            ("MAX_FILE_BYTES", "LOCAL_MAX_FILE_BYTES"),
        ):
            with self.subTest(constant=contract_name):
                self.assertEqual(
                    getattr(workspace_contract, contract_name),
                    getattr(core, local_name),
                    f"{contract_name} and {local_name} disagree: the same file "
                    "would read differently depending on whether a Runtime exists",
                )


class WorkspaceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="w4-volume-")
        self.addCleanup(self.tempdir.cleanup)
        self.root = pathlib.Path(self.tempdir.name)
        self.workspace = WORKSPACE
        self.ws_dir = self.root / self.workspace
        (self.ws_dir / "src").mkdir(parents=True)
        (self.ws_dir / ".sandbox").mkdir()
        (self.ws_dir / ".sandbox" / "meta.json").write_text("{}", encoding="utf-8")
        (self.ws_dir / "src" / "main.py").write_text(
            "line1\nline2\nline3\n", encoding="utf-8"
        )
        previous = core.WORKSPACE_VOLUME_ROOT
        self.addCleanup(setattr, core, "WORKSPACE_VOLUME_ROOT", previous)
        core.WORKSPACE_VOLUME_ROOT = str(self.root)


class PathSanitisationTests(WorkspaceFixture):
    def test_traversal_and_absolute_paths_are_refused(self) -> None:
        # Refused by this function, not by something further down that happens
        # to raise the same type.
        for bad in ("../etc/passwd", "../../..", "/etc/passwd", "src/../../x"):
            with self.subTest(path=bad):
                with self.assertRaisesRegex(ValueError, "path escapes workspace"):
                    volume.local_safe_path(self.workspace, bad)

    def test_the_reserved_metadata_directory_is_refused(self) -> None:
        # .sandbox holds lifecycle metadata. Reaching it means the reader can
        # rewrite last_used_at, that is, decide when its own workspace is
        # collected.
        with self.assertRaises(ValueError):
            volume.local_safe_path(self.workspace, ".sandbox/meta.json")

    def test_a_symlink_that_escapes_after_resolution_is_refused(self) -> None:
        """The case a string check cannot see.

        There is no ``..`` anywhere in this path, and every component is a
        legal name. The escape only exists once the link is resolved, so the
        criterion has to be "resolve, then verify with relative_to".
        """
        (self.ws_dir / "escape").symlink_to("/etc")
        with self.assertRaises(ValueError):
            volume.local_safe_path(self.workspace, "escape/passwd")

    def test_a_symlink_that_stays_inside_is_still_allowed(self) -> None:
        # The guard has to refuse escapes, not every link: a check that fails
        # both cases would pass the test above while breaking normal use.
        (self.ws_dir / "inside").symlink_to(self.ws_dir / "src")
        resolved = volume.local_safe_path(self.workspace, "inside/main.py")
        self.assertEqual(resolved, (self.ws_dir / "src" / "main.py").resolve())

    def test_a_nul_byte_is_refused_deliberately(self) -> None:
        """The message matters: a bare assertRaises cannot judge this.

        Deleting the explicit check does not let a NUL through -- ``resolve()``
        raises ``ValueError("embedded null byte")`` on the way past, the same
        type. Asserting only the type would call an incidental crash a working
        guard, so the criterion is which of the two answered.
        """
        with self.assertRaisesRegex(ValueError, "path must be a string"):
            volume.local_safe_path(self.workspace, "src/ma\x00in.py")

    def test_a_workspace_id_that_is_itself_a_traversal_is_refused(self) -> None:
        # The id is joined onto the volume root before any path check runs, so
        # it needs its own.
        with self.assertRaises(ValueError):
            volume.local_safe_path("../other", "src/main.py")

    def test_the_root_is_addressable_only_when_the_caller_allows_it(self) -> None:
        self.assertEqual(
            volume.local_safe_path(self.workspace, ".", allow_root=True),
            self.ws_dir.resolve(),
        )
        with self.assertRaises(ValueError):
            volume.local_safe_path(self.workspace, ".")


class LocalListTests(WorkspaceFixture):
    def test_the_listing_hides_the_metadata_directory(self) -> None:
        view = volume.local_list_files(self.workspace, ".")
        self.assertEqual(view["workspace_id"], self.workspace)
        self.assertEqual(view["path"], ".")
        self.assertFalse(view["truncated"])
        names = {entry["name"] for entry in view["entries"]}
        self.assertIn("src", names)
        self.assertNotIn(".sandbox", names, ".sandbox must not appear in a listing")

    def test_entry_types_use_the_agreed_vocabulary(self) -> None:
        view = volume.local_list_files(self.workspace, "src")
        self.assertEqual(view["entries"], [{"name": "main.py", "type": "file"}])

    def test_truncation_is_reported_rather_than_silent(self) -> None:
        many = self.ws_dir / "many"
        many.mkdir()
        for index in range(core.LOCAL_MAX_LIST_ENTRIES + 5):
            (many / f"f{index:04d}").write_text("", encoding="utf-8")
        view = volume.local_list_files(self.workspace, "many")
        self.assertTrue(view["truncated"])
        self.assertEqual(len(view["entries"]), core.LOCAL_MAX_LIST_ENTRIES)

    def test_a_file_is_not_listable_as_a_directory(self) -> None:
        with self.assertRaises(ValueError):
            volume.local_list_files(self.workspace, "src/main.py")


class LocalReadTests(WorkspaceFixture):
    def test_a_small_file_is_returned_whole(self) -> None:
        view = volume.local_read_file(self.workspace, "src/main.py")
        self.assertEqual(view["content"], "line1\nline2\nline3\n")
        self.assertEqual((view["start_line"], view["end_line"]), (1, 3))
        self.assertFalse(view["truncated"])
        # Offering a next offset when nothing was cut would send the caller
        # back for a page that does not exist.
        self.assertNotIn("next_offset", view)

    def test_the_offset_and_limit_window_reports_where_to_resume(self) -> None:
        view = volume.local_read_file(self.workspace, "src/main.py", offset=2, limit=1)
        self.assertEqual(view["content"], "line2\n")
        self.assertEqual((view["start_line"], view["end_line"]), (2, 2))
        self.assertTrue(view["truncated"])
        self.assertEqual(view["next_offset"], 3)

    def test_an_offset_past_the_end_is_an_error_not_an_empty_read(self) -> None:
        # An empty body would read as "the file is empty", which is a different
        # fact from "you asked past the end".
        with self.assertRaises(ValueError):
            volume.local_read_file(self.workspace, "src/main.py", offset=99)

    def test_a_single_long_line_is_cut_and_the_cut_is_declared(self) -> None:
        long_line = "x" * (core.LOCAL_MAX_READ_CHARS + 500) + "\n"
        (self.ws_dir / "long.txt").write_text(long_line, encoding="utf-8")
        view = volume.local_read_file(self.workspace, "long.txt")
        self.assertEqual(len(view["content"]), core.LOCAL_MAX_READ_CHARS)
        self.assertTrue(view["truncated"])
        # These two fields are the contract: without them the caller believes
        # it holds the whole line.
        self.assertEqual(view["clipped_line"], 1)
        self.assertEqual(view["clipped_length"], len(long_line))

    def test_a_binary_file_names_the_encoding_rather_than_failing_vaguely(self) -> None:
        (self.ws_dir / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
        with self.assertRaises(ValueError) as caught:
            volume.local_read_file(self.workspace, "blob.bin")
        self.assertIn("UTF-8", str(caught.exception))

    def test_a_directory_is_not_readable_as_a_file(self) -> None:
        with self.assertRaises(ValueError):
            volume.local_read_file(self.workspace, "src")


class VolumeAbsentTests(unittest.TestCase):
    def test_workspace_dir_reports_offline_when_no_volume_is_mounted(self) -> None:
        previous = core.WORKSPACE_VOLUME_ROOT
        self.addCleanup(setattr, core, "WORKSPACE_VOLUME_ROOT", previous)
        core.WORKSPACE_VOLUME_ROOT = ""
        with self.assertRaises(core.WorkspaceOffline):
            volume.workspace_dir(WORKSPACE)


if __name__ == "__main__":
    unittest.main()
