#!/usr/bin/env python3
"""Run a reverse experiment: break one thing on purpose, prove a test catches it.

A test that has never been seen to fail is a test nobody has evidence for. The
procedure is to make the change the test is supposed to catch, confirm the test
goes red, restore, and confirm it goes green again - and to record both exit
codes rather than the conclusion drawn from them.

The interesting part is not running the experiment; it is the five ways of
getting a *wrong* answer that this refuses to produce. Each refusal exists
because the failure it prevents is indistinguishable from a real result:

1. an empty or no-op mutated form - a mutation that cannot be undone, and whose
   "restored" run is therefore measuring the mutated tree;
2. an apply anchor matching zero or several places - the mutation lands
   somewhere other than where it was meant to, and the red is about something
   else;
3. a baseline that is already red - the failure is inherited, and the experiment
   reports somebody else's problem as this change's, sending whoever reads it to
   fix a thing that is not broken;
4. a restore that did not reproduce the original bytes - the rest of the session
   runs on a damaged tree, and every later "restored" reading is meaningless;
5. a third party writing the file while the experiment runs - restoring would
   blind-write over their work, silently, and the tree would still look fine.

🔴 Four and five are the reason the restore is verified by content hash rather
than by searching for the mutated text and putting the original back. A reverse
search can match the wrong occurrence, or none, and report success either way; a
hash comparison answers the actual question, which is whether the file came back.

This tool is deliberately local to this repository. Sharing one copy across
projects would add exactly the kind of dependency this project is separating
out. The rules above are worth copying; the file is not.

Usage:
    python3 scripts/mutation-experiment.py experiments.json

where the file is a list of objects with: name, path, old, new, module.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clear_bytecode() -> None:
    # Stale .pyc after a restore is its own way of reading the wrong tree.
    subprocess.run(
        ["find", str(ROOT), "-name", "__pycache__", "-type", "d", "-prune",
         "-exec", "rm", "-rf", "{}", "+"],
        check=False, capture_output=True,
    )


def run_tests(module: str) -> int:
    clear_bytecode()
    return subprocess.run(
        [sys.executable, "-m", "unittest", module],
        cwd=ROOT, capture_output=True, text=True, timeout=1800,
    ).returncode


def experiment(
    name: str, relative: str, old: str, new: str, module: str, *, run=run_tests
) -> tuple[str, str, str]:
    """Return ``(name, verdict, detail)``; verdict is OK, PROBLEM, REFUSED or ABORTED."""
    path = ROOT / relative
    if not new.strip() or new == old:
        return name, "REFUSED", "mutated form is empty or a no-op: unrevertable"
    source = path.read_text(encoding="utf-8")
    occurrences = source.count(old)
    if occurrences != 1:
        return name, "REFUSED", f"apply anchor matches {occurrences} places, need 1"
    baseline = run(module)
    if baseline != 0:
        return name, "REFUSED", (
            f"baseline is already red (exit {baseline}); a mutation on top of it "
            "would report an inherited failure as this change's"
        )
    original = digest(source)
    mutated_text = source.replace(old, new)
    path.write_text(mutated_text, encoding="utf-8")
    try:
        mutated = run(module)
    finally:
        if path.read_text(encoding="utf-8") != mutated_text:
            return name, "ABORTED", (
                "the file changed while the experiment ran; refusing to restore "
                "over a concurrent writer"
            )
        path.write_text(source, encoding="utf-8")
    if digest(path.read_text(encoding="utf-8")) != original:
        return name, "ABORTED", "restore did not reproduce the original bytes"
    restored = run(module)
    verdict = "OK" if mutated != 0 and restored == 0 else "PROBLEM"
    return name, verdict, f"mutated={mutated} restored={restored}"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    cases = json.loads(pathlib.Path(argv[1]).read_text(encoding="utf-8"))
    failures = 0
    for case in cases:
        name, verdict, detail = experiment(
            case["name"], case["path"], case["old"], case["new"], case["module"]
        )
        print(f"{verdict:8} {detail:34} {name}", flush=True)
        if verdict != "OK":
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
