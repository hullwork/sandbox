from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from sandbox_platform import sandbox_cli
from sandbox_platform.sandbox_client import CommandResult, ControlPlaneError


class SandboxCliTests(unittest.TestCase):
    def test_exec_forwards_streams_and_exit_code(self) -> None:
        sandbox = mock.Mock()
        sandbox.run_command.return_value = CommandResult(
            exit_code=9,
            stdout="out\n",
            stderr="err\n",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(sandbox_cli.Sandbox, "get", return_value=sandbox) as get,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = sandbox_cli.main(["exec", "demo", "--", "printf", "ok"])
        self.assertEqual(code, 9)
        self.assertEqual(stdout.getvalue(), "out\n")
        self.assertEqual(stderr.getvalue(), "err\n")
        get.assert_called_once_with("demo", resume=True)
        sandbox.run_command.assert_called_once_with(
            "printf", ["ok"], timeout_seconds=30
        )

    def test_run_stop_releases_runtime_even_when_command_fails(self) -> None:
        sandbox = mock.Mock()
        sandbox.run_command.return_value = CommandResult(
            exit_code=3,
            stdout="",
            stderr="",
        )
        with mock.patch.object(
            sandbox_cli.Sandbox, "get_or_create", return_value=sandbox
        ):
            code = sandbox_cli.main(
                ["run", "--name", "demo", "--stop", "--", "false"]
            )
        self.assertEqual(code, 3)
        sandbox.stop.assert_called_once_with()

    def test_a_failed_stop_is_reported_but_keeps_the_command_exit_code(self) -> None:
        # The command ran and exited 0; its output has already gone out. A stop
        # failure replacing that 0 with 1 would tell the caller the command
        # failed, which is the one thing that did not happen.
        sandbox = mock.Mock()
        sandbox.run_command.return_value = CommandResult(
            exit_code=0, stdout="ok\n", stderr=""
        )
        sandbox.stop.side_effect = ControlPlaneError(502, "control plane away")
        stderr = io.StringIO()
        with (
            mock.patch.object(
                sandbox_cli.Sandbox, "get_or_create", return_value=sandbox
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            code = sandbox_cli.main(["run", "--name", "demo", "--stop", "--", "true"])
        self.assertEqual(code, 0)
        self.assertIn("stop failed: control plane away", stderr.getvalue())
        sandbox.stop.assert_called_once_with()

    def test_missing_command_is_a_clean_cli_error(self) -> None:
        with (
            mock.patch.object(sandbox_cli.Sandbox, "get") as get,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            code = sandbox_cli.main(["exec", "demo"])
        self.assertEqual(code, 1)
        self.assertIn("a command is required", stderr.getvalue())
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
