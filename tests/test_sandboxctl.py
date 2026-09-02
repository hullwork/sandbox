from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from sandbox_platform import sandboxctl


class _Stdin(io.StringIO):
    def __init__(self, text: str = "", *, tty: bool) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class ReleaseConfirmationTests(unittest.TestCase):
    """``release`` deletes any tenant's Runtime with the admin token.

    The criterion is whether the DELETE went out, not what was printed.
    """

    def release(self, argv: list[str], stdin: _Stdin) -> tuple[int, mock.Mock, str]:
        stderr = io.StringIO()
        with (
            mock.patch.object(sandboxctl, "request", return_value={}) as request,
            mock.patch.object(sandboxctl.sys, "stdin", stdin),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            code = sandboxctl.main(argv)
        return code, request, stderr.getvalue()

    def test_without_a_terminal_release_requires_yes(self) -> None:
        code, request, stderr = self.release(
            ["release", "sb-000000000001"], _Stdin(tty=False)
        )
        self.assertEqual(code, 2)
        self.assertIn("--yes", stderr)
        request.assert_not_called()

    def test_yes_releases_without_asking(self) -> None:
        code, request, _ = self.release(
            ["release", "sb-000000000001", "--yes"], _Stdin(tty=False)
        )
        self.assertEqual(code, 0)
        request.assert_called_once_with("DELETE", "/v1/sandboxes/sb-000000000001")

    def test_a_declined_prompt_releases_nothing(self) -> None:
        code, request, stderr = self.release(
            ["release", "sb-000000000001"], _Stdin("n\n", tty=True)
        )
        self.assertEqual(code, 2)
        self.assertIn("aborted", stderr)
        request.assert_not_called()

    def test_an_accepted_prompt_releases(self) -> None:
        code, request, _ = self.release(
            ["release", "sb-000000000001"], _Stdin("y\n", tty=True)
        )
        self.assertEqual(code, 0)
        request.assert_called_once_with("DELETE", "/v1/sandboxes/sb-000000000001")


if __name__ == "__main__":
    unittest.main()
