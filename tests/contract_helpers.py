"""Small parsers shared by standalone image and build-contract tests."""

from __future__ import annotations

import ast
import pathlib
import posixpath
import shlex
import subprocess


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def dockerfile_instructions(text: str) -> list[tuple[str, str]]:
    merged: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        buffer += line
        merged.append(buffer)
        buffer = ""
    if buffer:
        merged.append(buffer)
    return [
        (head.upper(), rest.strip())
        for line in merged
        for head, _, rest in [line.partition(" ")]
    ]


def copy_sources(text: str) -> set[str]:
    sources: set[str] = set()
    for name, args in dockerfile_instructions(text):
        if name != "COPY":
            continue
        tokens = shlex.split(args)
        if any(token.startswith("--from=") for token in tokens):
            continue
        tokens = [token for token in tokens if not token.startswith("--")]
        if len(tokens) >= 2:
            sources.update(tokens[:-1])
    return sources


def image_contains(sources: set[str], filename: str) -> bool:
    for source in sources:
        if source in {".", "./"}:
            return True
        if source.endswith("/") and (REPO_ROOT / source / filename).exists():
            return True
        if posixpath.basename(source) == filename:
            return True
    return False


def first_party_imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    local = {candidate.stem for candidate in REPO_ROOT.glob("*.py")}
    local |= {candidate.stem for candidate in path.parent.glob("*.py")}
    return {name for name in names if name in local}


def tracked_files(*patterns: str) -> list[str]:
    names = subprocess.run(
            ["git", "ls-files", *patterns],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    # `git ls-files` includes paths deleted in the working tree. Contract tests
    # inspect the current candidate tree, so staged/unstaged deletions must not
    # be reopened as if they still existed.
    return sorted(name for name in names if (REPO_ROOT / name).is_file())


def join_continuations(text: str) -> list[str]:
    merged: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        merged.append(buffer + line)
        buffer = ""
    if buffer:
        merged.append(buffer)
    return merged
