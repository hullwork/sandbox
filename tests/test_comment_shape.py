"""A ratchet on the machine-translated comment blocks.

The 2026-09-02 review found 375 comment lines whose `#` is followed directly
by a letter, a quote or a parenthesis. That shape is the signature of the
machine-translated paragraphs (a human writing English puts a space after
`#`), and the paragraphs it marks are the ones a reader cannot recover the
meaning from - the `/healthz` block read "the physical examination should
truthfully say". Word lists do not find these; the shape does.

The count may only go down. Whoever back-translates a block lowers the
threshold in the same change; a change that adds such a line fails here.
`#!` (shebang) and `#:` (Sphinx-style attribute comments) are excluded, as in
the review's own count, so the number here is comparable to the number there.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SUFFIXES = {".py", ".sh", ".yaml"}
SHAPE = re.compile(r'^\s*#[A-Za-z"(]')
EXCLUDED = re.compile(r"#!/|#:")
# 375 at the review; 353 after the first back-translation pass (the /healthz
# block in control_plane/api.py and the timing constants in
# runtime/shell_sessions.py). Lower it when you translate more; never raise it.
CEILING = 353


def tracked_sources() -> list[pathlib.Path]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [
        ROOT / name for name in listing.split("\0")
        if name and pathlib.Path(name).suffix in SUFFIXES
    ]


def spaceless_comment_lines() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in tracked_sources():
        hits = sum(
            1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if SHAPE.match(line) and not EXCLUDED.search(line)
        )
        if hits:
            counts[str(path.relative_to(ROOT))] = hits
    return counts


class CommentShapeTests(unittest.TestCase):
    def test_the_scan_sees_tracked_sources(self) -> None:
        self.assertGreater(len(tracked_sources()), 100)

    def test_spaceless_comment_lines_do_not_grow(self) -> None:
        counts = spaceless_comment_lines()
        total = sum(counts.values())
        worst = sorted(counts.items(), key=lambda item: -item[1])[:8]
        self.assertLessEqual(
            total, CEILING,
            f"{total} comment lines with no space after '#', ceiling is {CEILING}; "
            f"largest files: {worst}",
        )


if __name__ == "__main__":
    unittest.main()
