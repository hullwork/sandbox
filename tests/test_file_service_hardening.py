"""The hardening aspects of File Service: retained directories, crash self-healing, logs, connection timeouts, and mirror contracts.

Cannot afford HTTP (except for one use case that really needs to verify socket behavior): directly construct ApiHandler and replace
send_json, the same method as tests/test_file_search.py. WORKSPACE is a module-level constant,
The test points to the temporary directory.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import socket
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

import yaml

# Reuse the shared Dockerfile and build-command parsers instead of matching a
# literal COPY command. Valid reordering must not create a false failure;
# otherwise the guard is likely to be disabled rather than fixed.
from tests.contract_helpers import (
    copy_sources,
    first_party_imports,
    image_contains,
    join_continuations,
    tracked_files,
)


def load_file_service():
    os.environ.setdefault("FILE_SERVICE_CAPABILITY_KEY", "test-capability-key")
    os.environ.setdefault("WORKSPACE_ID", "ws-123456789abc")
    path = pathlib.Path(__file__).resolve().parents[1] / "file-service/file_service.py"
    spec = importlib.util.spec_from_file_location("hardening_file_service", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


file_service = load_file_service()
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FILE_SERVICE_DIR = REPO_ROOT / "file-service"


def file_service_build_commands() -> dict[str, list[str]]:
    """Collect every ``docker build`` line in the repo that builds the file-service image, grouped by file.

    Discover call sites rather than copying a current list, and join continued
    lines before matching. Otherwise a valid multi-line docker build command is
    mistaken for a missing call site.
    """
    hits: dict[str, list[str]] = {}
    for name in tracked_files("*.sh", "*.yml", "*.yaml", "Makefile"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        for line in join_continuations(text):
            if line.lstrip().startswith("#") or not (
                "docker build" in line or "scripts/build-image.sh" in line
            ):
                continue
            if "file-service/Dockerfile" in line or "file_service_image" in line:
                hits.setdefault(name, []).append(line)
    return hits


def first_party_package_imports(path: pathlib.Path) -> set[str]:
    """Repository files a ``from <package> import <module>`` needs in the image.

    ``first_party_imports`` only knows flat root modules. ``safe_stdout`` now
    lives in the ``sandbox_platform`` package, so the image must carry both
    ``sandbox_platform/__init__.py`` and ``sandbox_platform/safe_stdout.py``;
    returning file paths (not module names) lets the guard check exactly those.
    """
    packages = {
        candidate.name
        for candidate in REPO_ROOT.iterdir()
        if candidate.is_dir() and (candidate / "__init__.py").exists()
    }
    needed: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module in packages:
            needed.add(f"{node.module}/__init__.py")
            for alias in node.names:
                if (REPO_ROOT / node.module / f"{alias.name}.py").exists():
                    needed.add(f"{node.module}/{alias.name}.py")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                package, _, module = alias.name.partition(".")
                if package in packages:
                    needed.add(f"{package}/__init__.py")
                    if module:
                        needed.add(f"{package}/{module}.py")
    return needed


def image_contains_path(sources: set[str], relative: str) -> bool:
    """Path-aware sibling of image_contains: a package file must be copied from its own directory."""
    for source in sources:
        if source in {".", "./"} or source == relative:
            return True
        if source.endswith("/") and relative.startswith(source):
            return True
    return False


def dockerfile_context_root(sources: set[str]) -> pathlib.Path:
    """The source path of COPY is written relative to which directory.

    Judge according to "Can it be parsed on the disk?" and do not look at the string prefix. and
    test_infra_contracts.dockerfile_context_root has the same judgment method, but the candidate root is written down there.
    into (RUNTIME_DIR, REPO_ROOT), which cannot be used for file-service.
    """
    discriminating = {s for s in sources if s not in {".", "./"}}
    candidates = [
        root
        for root in (FILE_SERVICE_DIR, REPO_ROOT)
        if discriminating and all((root / s).exists() for s in discriminating)
    ]
    if len(candidates) != 1:
        raise AssertionError(
            f"cannot determine the file-service Dockerfile context (sources {sorted(sources)} "
            f"resolve under {len(candidates)} candidate roots; the test is ambiguous"
        )
    return candidates[0]


class WorkspaceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.original_workspace = file_service.WORKSPACE
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name).resolve()
        file_service.WORKSPACE = self.root

    def tearDown(self) -> None:
        file_service.WORKSPACE = self.original_workspace
        self._tmp.cleanup()


class ReservedDirectoryTests(WorkspaceCase):
    """The retention criteria for`.sandbox`must prevent "walking around the name"."""

    def test_a_symlink_named_something_else_cannot_reach_sandbox(self) -> None:
        # When the criterion is done before resolve(), it is allowed if the first paragraph is "link", and it falls in the
        # .sandbox and still in the workspace —— ln -s in the sandbox will change the Control Plane
        # write_file becomes a tool for writing.sandbox.
        (self.root / ".sandbox").mkdir()
        os.symlink(".sandbox", self.root / "link")
        with self.assertRaises(ValueError):
            file_service.safe_path("link/last_used_at")

    def test_a_symlinked_parent_deeper_in_the_tree_cannot_reach_sandbox(self) -> None:
        # Not just the first paragraph: any middle paragraph that is a symbolic link can be dropped in.
        (self.root / ".sandbox").mkdir()
        (self.root / "a").mkdir()
        os.symlink(self.root / ".sandbox", self.root / "a" / "b")
        with self.assertRaises(ValueError):
            file_service.safe_path("a/b/restore-1/payload")

    def test_naming_sandbox_directly_is_still_rejected(self) -> None:
        with self.assertRaises(ValueError):
            file_service.safe_path(".sandbox/last_used_at")

    def test_a_directory_merely_named_like_a_prefix_is_allowed(self) -> None:
        # The criterion is that path segments are equal, not prefix matching -.sandboxes/ is the user's normal directory.
        self.assertEqual(
            file_service.safe_path(".sandboxes/notes.txt"),
            self.root / ".sandboxes/notes.txt",
        )

    def test_ordinary_paths_still_resolve(self) -> None:
        self.assertEqual(
            file_service.safe_path("docs/readme.md"), self.root / "docs/readme.md"
        )

    def test_escaping_the_workspace_is_still_rejected(self) -> None:
        with self.assertRaises(ValueError):
            file_service.safe_path("../outside.txt")


class RestoreCrashRecoveryTests(WorkspaceCase):
    """After the restore is SIGKILLed midway, the next startup must be able to harvest the workspace into a complete tree.

    SIGKILL cannot reach the except of`_swap_in_restored_tree`, so it is completely expected to roll back that section.
    Not; and Runtime's /activity only counts PTY sessions, and a restore of 60 seconds is running at a time.
    The probation criteria of the Control Plane do not exist at all. When the TTL reaches the point, gracePeriodSeconds: 0 is used to delete the Pod.
    """

    def setUp(self) -> None:
        super().setUp()
        self.metadata = self.root / ".sandbox"
        self.metadata.mkdir()

    def stage(self, phase: str, *, workspace: dict, retired: dict, staging: dict) -> None:
        """Manually display the disk status + log according to a certain phase to simulate "the process is forcibly killed here"."""
        for name, body in workspace.items():
            (self.root / name).write_text(body, encoding="utf-8")
        self.retired = self.metadata / "old-abc"
        self.staging = self.metadata / "restore-xyz"
        for directory, files in ((self.retired, retired), (self.staging, staging)):
            directory.mkdir()
            for name, body in files.items():
                (directory / name).write_text(body, encoding="utf-8")
        file_service._write_restore_journal(phase, self.staging, self.retired)

    def tree(self) -> dict:
        return {
            path.name: path.read_text(encoding="utf-8")
            for path in self.root.iterdir()
            if path.name != ".sandbox"
        }

    def test_a_crash_before_anything_was_installed_rolls_back(self) -> None:
        # retiring: Half of the old tree was moved, and none of the new tree came in. ⇒ The old tree must be completely restored.
        self.stage(
            "retiring",
            workspace={"kept.txt": "old-kept"},
            retired={"moved.txt": "old-moved"},
            staging={"kept.txt": "new-kept", "fresh.txt": "new-fresh"},
        )
        self.assertEqual(
            file_service.recover_interrupted_restore(),
            {"restore_recovery": "rolled_back", "entries": 1},
        )
        self.assertEqual(self.tree(), {"kept.txt": "old-kept", "moved.txt": "old-moved"})
        self.assertFalse(self.staging.exists())
        self.assertFalse(self.retired.exists())
        self.assertFalse((self.metadata / file_service.RESTORE_JOURNAL_NAME).exists())

    def test_a_crash_halfway_through_installing_rolls_forward(self) -> None:
        # installing: The old tree has been removed as a whole, and the new tree is half installed in the workspace ⇒ Put the remaining
        # Finished. At this time, going back will require two more rounds of rename, and this is the result that the caller wants.
        self.stage(
            "installing",
            workspace={"a.txt": "new-a"},
            retired={"a.txt": "old-a", "b.txt": "old-b"},
            staging={"b.txt": "new-b"},
        )
        self.assertEqual(
            file_service.recover_interrupted_restore(),
            {"restore_recovery": "rolled_forward", "entries": 1},
        )
        self.assertEqual(self.tree(), {"a.txt": "new-a", "b.txt": "new-b"})
        self.assertFalse(self.staging.exists())
        self.assertFalse(self.retired.exists())

    def test_an_orphan_old_directory_without_a_journal_is_left_alone(self) -> None:
        # When the rollback also fails,`_swap_in_restored_tree`is**deliberately**leaving old-* behind:
        # That's the only remaining copy of the user's data. Self-healing and clearing it will really make it gone.
        orphan = self.metadata / "old-orphan"
        orphan.mkdir()
        (orphan / "only-copy.txt").write_text("precious", encoding="utf-8")
        self.assertIsNone(file_service.recover_interrupted_restore())
        self.assertEqual((orphan / "only-copy.txt").read_text(encoding="utf-8"), "precious")

    def test_a_journal_naming_a_directory_outside_sandbox_is_refused(self) -> None:
        # Log files live on the tenant volume. Without verifying the directory name, a "../.." will allow self-healing
        # rmtree hits outside of.sandbox.
        keep = self.root / "keep.txt"
        keep.write_text("keep", encoding="utf-8")
        journal = self.metadata / file_service.RESTORE_JOURNAL_NAME
        journal.write_text(
            json.dumps({"phase": "retiring", "staging": "../..", "retired": ".."}),
            encoding="utf-8",
        )
        self.assertIsNone(file_service.recover_interrupted_restore())
        self.assertTrue(keep.exists())
        self.assertFalse(journal.exists())

    def test_no_journal_means_nothing_to_do(self) -> None:
        self.assertIsNone(file_service.recover_interrupted_restore())

    def test_the_swap_journals_before_it_touches_the_workspace(self) -> None:
        """Whether the self-healing can be completed depends entirely on whether the log is placed before the first rename.

        This is about accessibility: the above use cases are manually placed in a state where only the production code is actually
        This order will only appear in reality when you write a log. Capture a log before each os.replace
        Snapshot, asserting that there is already a log for the first time, and the phase changes with the progress.
        """
        staging = self.metadata / "restore-xyz"
        retired = self.metadata / "old-abc"
        staging.mkdir()
        retired.mkdir()
        (self.root / "old.txt").write_text("old", encoding="utf-8")
        (staging / "new.txt").write_text("new", encoding="utf-8")

        journal = self.metadata / file_service.RESTORE_JOURNAL_NAME
        phases: list[str | None] = []
        real_replace = os.replace

        def spy(source, destination):
            # The log itself is also written by atomic_write (it is internally os.replace), here only
            # Those times when you are concerned about "touching user data".
            if pathlib.Path(destination).name != file_service.RESTORE_JOURNAL_NAME:
                try:
                    phases.append(json.loads(journal.read_text(encoding="utf-8"))["phase"])
                except (OSError, ValueError, KeyError):
                    phases.append(None)
            real_replace(source, destination)

        os.replace = spy
        try:
            file_service.ApiHandler._swap_in_restored_tree(staging, retired)
        finally:
            os.replace = real_replace

        # Two renames: first move old.txt into retired, and then load new.txt into the workspace.
        self.assertEqual(phases, ["retiring", "installing"])
        self.assertFalse(journal.exists(), "successful swap must remove the journal")
        self.assertEqual(self.tree(), {"new.txt": "new"})

    def test_a_failed_swap_leaves_no_journal_behind(self) -> None:
        # Rollback successful = the disk is back to its original state = nothing that needs to be finished on the next startup.
        staging = self.metadata / "restore-xyz"
        retired = self.metadata / "old-abc"
        staging.mkdir()
        retired.mkdir()
        (self.root / "old.txt").write_text("old", encoding="utf-8")
        (staging / "new.txt").write_text("new", encoding="utf-8")

        real_replace = os.replace
        calls = {"n": 0}

        def flaky(source, destination):
            calls["n"] += 1
            if calls["n"] == 2:  # Exploded when loading the first new file
                raise OSError("no space left on device")
            real_replace(source, destination)

        os.replace = flaky
        try:
            with self.assertRaises(OSError):
                file_service.ApiHandler._swap_in_restored_tree(staging, retired)
        finally:
            os.replace = real_replace

        self.assertEqual(self.tree(), {"old.txt": "old"})
        self.assertFalse((self.metadata / file_service.RESTORE_JOURNAL_NAME).exists())


class RequestLogTests(unittest.TestCase):
    """Access logs should not bring user paths and search terms into container logs."""

    def _handler(self):
        handler = object.__new__(file_service.ApiHandler)
        handler.address_string = lambda: "127.0.0.1"
        return handler

    def _log(self, requestline: str) -> str:
        handler = self._handler()
        handler.requestline = requestline
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            handler.log_request(200, 12)
        return buffer.getvalue()

    def test_the_grep_pattern_never_reaches_stdout(self) -> None:
        line = self._log('GET /v1/files/grep?pattern=AKIA_SECRET&path=notes HTTP/1.1')
        self.assertNotIn("AKIA_SECRET", line)
        self.assertNotIn("pattern=", line)

    def test_the_requested_path_never_reaches_stdout(self) -> None:
        line = self._log('GET /v1/files/read?path=clients/acme/contract.md HTTP/1.1')
        self.assertNotIn("acme", line)
        self.assertNotIn("contract.md", line)

    def test_what_is_left_still_identifies_the_request(self) -> None:
        # Desensitization is not "nothing": methods, endpoints, protocols, and status codes must be retained, otherwise the logs
        # There is no troubleshooting value other than the row count.
        line = self._log('GET /v1/files/read?path=secret.md HTTP/1.1')
        self.assertIn("GET", line)
        self.assertIn("/v1/files/read", line)
        self.assertIn("HTTP/1.1", line)
        self.assertIn("200", line)
        self.assertIn("127.0.0.1", line)

    def test_a_malformed_request_line_is_redacted_too(self) -> None:
        # log_error also uses log_message, and it prints the original request line.
        handler = self._handler()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            handler.log_error("code %d, message %s", 400, "Bad request syntax ('?path=secret.md')")
        self.assertNotIn("secret.md", buffer.getvalue())


class DeliveryArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.original_workspace = file_service.WORKSPACE
        file_service.WORKSPACE = pathlib.Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        file_service.WORKSPACE = self.original_workspace
        self.temp.cleanup()

    def _archive(self, path: str = "artifacts") -> bytes:
        handler = object.__new__(file_service.ApiHandler)
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: self.assertEqual(status, 200)
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        handler.handle_archive(path)
        return handler.wfile.getvalue()

    def test_bundle_has_manifest_and_relative_regular_files(self) -> None:
        root = file_service.WORKSPACE / "artifacts"
        (root / "reports").mkdir(parents=True)
        (root / "reports" / "summary.md").write_text("done\n", encoding="utf-8")
        payload = self._archive()
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            self.assertEqual(archive.getnames(), ["reports/summary.md", "manifest.json"])
            manifest = json.load(archive.extractfile("manifest.json"))
        self.assertEqual(manifest["file_count"], 1)
        self.assertEqual(manifest["files"][0]["path"], "reports/summary.md")

    def test_bundle_excludes_internal_state_symlinks_and_legacy_compaction(self) -> None:
        root = file_service.WORKSPACE / "artifacts"
        (root / "compaction").mkdir(parents=True)
        (root / "compaction" / "prompt.json").write_text("secret", encoding="utf-8")
        (root / ".agent-state").mkdir()
        (root / ".agent-state" / "trace").write_text("secret", encoding="utf-8")
        (root / "result.txt").write_text("ok", encoding="utf-8")
        (root / "link").symlink_to(root / "result.txt")
        payload = self._archive()
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            names = archive.getnames()
        self.assertEqual(names, ["result.txt", "manifest.json"])


class ConnectionTimeoutTests(unittest.TestCase):
    """A silent connection cannot occupy a thread permanently."""

    def test_the_handler_declares_a_socket_timeout(self) -> None:
        # socketserver only setstimeout if timeout is not None; default is None,
        # That is, rfile.read() blocks infinitely.
        self.assertIsNotNone(file_service.ApiHandler.timeout)
        self.assertGreater(file_service.ApiHandler.timeout, 0)

    def test_a_client_that_never_finishes_its_request_line_gets_dropped(self) -> None:
        """Prove that this class attribute is really used by socketserver, and is not just an existing constant.

        Reduce the production value to within 0.3 seconds before running - no one will keep a use case that waits for a real 30 seconds.
        min() is intentionally coupled: if the timeout is removed from the production code, min(None,...)
        Report an error directly instead of secretly using your own value to support the use case.
        """
        impatient = type(
            "ImpatientHandler",
            (file_service.ApiHandler,),
            {
                "timeout": min(file_service.ApiHandler.timeout, 0.3),
                "log_message": lambda self, fmt, *args: None,
            },
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), impatient)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = socket.create_connection(server.server_address, timeout=5)
        try:
            client.sendall(b"GET /healthz")  # No line breaks: the server will stop at readline
            client.settimeout(5)
            started = time.monotonic()
            self.assertEqual(client.recv(1024), b"", "server should close the connection after timeout")
            self.assertLess(time.monotonic() - started, 4)
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class ImageContractTests(unittest.TestCase):
    """The image must really have these things - such missing items cannot be caught by a single test."""

    def dockerfile(self) -> str:
        return (REPO_ROOT / "file-service/Dockerfile").read_text(
            encoding="utf-8"
        )  # 9.3.3: Real standalone request body

    def test_pid_one_is_an_init_so_sigterm_is_not_ignored(self) -> None:
        # When python directly sets PID 1, the kernel ignores the SIG_DFL signal ⇒ docker stop /
        # All kubelet's SIGTERMs failed, and each time I had to wait for the grace period to expire before SIGKILL.
        text = self.dockerfile()
        self.assertRegex(text, r"apk add[^\n]*tini=", "tini must be installed with a pinned version")
        self.assertIn('ENTRYPOINT ["/sbin/tini", "--",', text)

    def test_every_module_it_imports_is_copied_into_the_image(self) -> None:
        """If you import the module of this repository, you must also bring it into the image.

        Tests run from the repository root, where `from sandbox_platform import
        safe_stdout` always resolves; if it is missing from the image, nothing
        explodes until the container starts. The criterion is the **instruction
        semantics** of copy_sources/image_contains, not the literal COPY line -
        see the note at the import section of the file header.
        """
        sources = copy_sources(self.dockerfile())
        imported: dict[str, set[str]] = {}
        for source in sorted(FILE_SERVICE_DIR.glob("*.py")):
            for module in first_party_imports(source):
                imported.setdefault(f"{module}.py", set()).add(source.name)
            for relative in first_party_package_imports(source):
                imported.setdefault(relative, set()).add(source.name)
        # Also guards the scan itself: the package import is known to exist. If it
        # cannot be scanned out, the parsing section is broken.
        self.assertIn(
            "sandbox_platform/safe_stdout.py",
            imported,
            "safe_stdout was not found; file_service.py __main__ should still import it",
        )
        self.assertIn("sandbox_platform/__init__.py", imported)
        for relative, users in sorted(imported.items()):
            with self.subTest(file=relative):
                contained = (
                    image_contains_path(sources, relative)
                    if "/" in relative
                    else image_contains(sources, relative)
                )
                self.assertTrue(
                    contained,
                    f"{sorted(users)} need {relative}, but it is absent from the image; "
                    "container startup would fail",
                )

    def test_a_legal_rewrite_of_the_copy_line_does_not_trip_the_guard(self) -> None:
        """What the guard recognizes is the semantics of the command, not what the line looks like.

        False positives are worse than no guards: the next person's response to "I wrote it correctly but it's red" is usually to turn it off.
        The following four ways of writing are**completely legal**(continue multiple lines / change uid / add other flags /
        The entire directory COPY), the guard must not be red.
        """
        legal = {
            "continued line": (
                "COPY --chown=65532:65532 \\\n"
                "    file-service/file_service.py \\\n"
                "    sandbox_platform/safe_stdout.py \\\n"
                "    /app/sandbox_platform/\n"
            ),
            "changed uid": "COPY --chown=1000:1000 sandbox_platform/safe_stdout.py /app/sandbox_platform/safe_stdout.py\n",
            "additional flag": "COPY --link --chown=65532:65532 sandbox_platform/safe_stdout.py /app/sandbox_platform/\n",
            "package directory copy": "COPY sandbox_platform/ /app/sandbox_platform/\n",
            "directory copy": "COPY . /app/\n",
        }
        for label, text in legal.items():
            with self.subTest(form=label):
                self.assertTrue(
                    image_contains_path(
                        copy_sources(text), "sandbox_platform/safe_stdout.py"
                    ),
                    f"{label} is valid but unrecognized; this is a false positive",
                )
        # A same-named file copied from anywhere else is not the package module.
        self.assertFalse(
            image_contains_path(
                copy_sources("COPY safe_stdout.py /app/safe_stdout.py\n"),
                "sandbox_platform/safe_stdout.py",
            )
        )

    def test_a_previous_build_stage_artefact_is_not_mistaken_for_a_repo_file(self) -> None:
        # COPY --from= copies an artifact of an earlier build stage, not a file from
        # the repository. Counting it would give a false positive for "the module is in
        # the image": the image really does carry that name, but the repository has no
        # file by that name at all. So it is not counted.
        staged = "COPY --from=builder sandbox_platform/safe_stdout.py /app/sandbox_platform/\n"
        self.assertFalse(
            image_contains_path(copy_sources(staged), "sandbox_platform/safe_stdout.py")
        )

    def test_every_build_entrypoint_agrees_with_the_dockerfile(self) -> None:
        """The source path shape of the Dockerfile must be consistent with the context of each build call point.

        safe_stdout.py lives under sandbox_platform/ at the repository root. As long as one call site still uses file-service/ as context, that path
        The built image has one less module. The missed error is
        `failed to compute cache key: "/file_service.py": not found`, no prompt at all
        "Wrong context selection"; and the set of use cases that read only the Dockerfile are still all green.

        What is asserted is**consistency**rather than a fixed writing method: green before turning, green after turning, and red only after turning halfway.
        By crucifying "must be -f....", this guard depends on "what the next person writes next".
        """
        root = dockerfile_context_root(copy_sources(self.dockerfile()))
        expects_dash_f = root == REPO_ROOT
        commands = file_service_build_commands()
        self.assertGreaterEqual(
            len(commands), 1, "no file-service build call site found; the test is invalid"
        )
        for path, lines in sorted(commands.items()):
            for line in lines:
                with self.subTest(path=path, command=line):
                    has_dash_f = (
                        "-f" in line.split() and "file-service/Dockerfile" in line
                    )
                    self.assertEqual(
                        has_dash_f,
                        expects_dash_f,
                        f"{path} context does not match Dockerfile source paths: sources are relative to "
                        f"{'the repository root' if expects_dash_f else 'file-service/'}, but this command "
                        f"{'does not' if expects_dash_f else 'unexpectedly'}"
                        " -f file-service/Dockerfile",
                    )

    def test_the_security_scan_matrix_agrees_too(self) -> None:
        """Verify the CI image matrix uses the Dockerfile's standalone context."""
        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        )
        entries = [
            entry
            for job in workflow["jobs"].values()
            for entry in (job.get("strategy", {}).get("matrix", {}) or {}).get("include", [])
            if entry.get("dockerfile") == "file-service/Dockerfile"
        ]
        self.assertEqual(len(entries), 1, "file-service matrix entry not found in ci.yml")
        root = dockerfile_context_root(copy_sources(self.dockerfile()))
        self.assertEqual(
            entries[0].get("context"),
            "." if root == REPO_ROOT else "file-service",
            "ci.yml context does not match Dockerfile source paths",
        )

    def test_every_context_copy_source_exists_in_the_standalone_repo(self) -> None:
        for source in sorted(copy_sources(self.dockerfile())):
            with self.subTest(source=source):
                self.assertTrue((REPO_ROOT / source).exists(), source)


if __name__ == "__main__":
    unittest.main()
