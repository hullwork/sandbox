"""Denied-access audit rows are throttled per (actor, action, target).

Every ownership denial wrote a row to ``sandbox_audit_log`` with no throttle
and no retention, so any tenant key looping over guessed ids could grow the
table without bound. ``DenialThrottle`` admits one row per key per window,
in-process and with bounded memory. It is a pure class, so it is compiled
out of ``core.py`` here without importing the module (which reads its
environment on import).
"""
from __future__ import annotations

import ast
import pathlib
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_throttle():
    source = (ROOT / "control_plane/core.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    body = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DenialThrottle"
    ]
    assert len(body) == 1, "core.py must define DenialThrottle once"
    namespace = {"threading": threading, "time": time}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "core.py", "exec"), namespace)
    return namespace["DenialThrottle"]


class DenialThrottleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.Throttle = load_throttle()

    def test_one_row_per_key_per_window(self) -> None:
        throttle = self.Throttle(60, 100)
        key = ("api-key", "k1", "workspace.access", "ws-a")
        self.assertTrue(throttle.admit(key, now=1000))
        self.assertFalse(throttle.admit(key, now=1001))
        self.assertFalse(throttle.admit(key, now=1059))
        self.assertTrue(throttle.admit(key, now=1060))

    def test_different_targets_and_actors_are_independent(self) -> None:
        throttle = self.Throttle(60, 100)
        self.assertTrue(throttle.admit(("k", "a", "workspace.access", "ws-a"), now=0))
        self.assertTrue(throttle.admit(("k", "a", "workspace.access", "ws-b"), now=0))
        self.assertTrue(throttle.admit(("k", "b", "workspace.access", "ws-a"), now=0))
        self.assertTrue(throttle.admit(("k", "a", "sandbox.access", "ws-a"), now=0))

    def test_memory_is_bounded_and_the_oldest_key_goes_first(self) -> None:
        throttle = self.Throttle(60, 3)
        for index in range(3):
            self.assertTrue(throttle.admit(("k", "a", "x", f"t{index}"), now=index))
        # A fourth key evicts t0 (the oldest); t0 is then admitted again - one
        # extra row for it, never a missing first row - which in turn evicts t1,
        # while t2 is still inside its window and still refused.
        self.assertTrue(throttle.admit(("k", "a", "x", "t3"), now=10))
        self.assertTrue(throttle.admit(("k", "a", "x", "t0"), now=11))
        self.assertFalse(throttle.admit(("k", "a", "x", "t2"), now=11))
        self.assertLessEqual(len(throttle._seen), 3)

    def test_invalid_parameters_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.Throttle(0, 10)
        with self.assertRaises(ValueError):
            self.Throttle(60, 0)


if __name__ == "__main__":
    unittest.main()
