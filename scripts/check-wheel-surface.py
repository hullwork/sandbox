#!/usr/bin/env python3
"""Refuse a wheel that would install anything but the published import surface.

A wheel's top-level entries land directly in the installing environment's
`site-packages`. Every name there is claimed globally: a distribution that ships
a flat `telemetry.py` breaks, and is broken by, any other project that ships one.
The names below are the only ones this project is allowed to claim, and the
comparison is equality - a wheel that stopped shipping the package is as wrong as
one that ships extra names, and a subset check would call that release good.
"""

from __future__ import annotations

import argparse
import sys
import zipfile


# Every top-level entry a wheel of this project may contain, `*.dist-info`
# aside. For a package directory this is also its import name.
ALLOWED_TOP_LEVEL = frozenset({"sandbox_platform"})


def top_level_entries(wheel: str) -> set[str]:
    """Return the wheel's top-level entries, excluding its `*.dist-info`.

    `*.data` is deliberately not excluded: its `purelib`/`platlib` payloads are
    installed into `site-packages` too, so a wheel that grows one has to be
    looked at by a person rather than waved through.
    """
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    entries = {name.split("/", 1)[0] for name in names}
    return {entry for entry in entries if not entry.endswith(".dist-info")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheels", nargs="+", help="built wheel files to check")
    arguments = parser.parse_args(argv)

    failures: list[str] = []
    for wheel in arguments.wheels:
        entries = top_level_entries(wheel)
        if entries == set(ALLOWED_TOP_LEVEL):
            print(f"{wheel}: top-level surface is {sorted(entries)}")
            continue
        unexpected = sorted(entries - ALLOWED_TOP_LEVEL)
        missing = sorted(ALLOWED_TOP_LEVEL - entries)
        failures.append(
            f"{wheel}: unexpected top-level entries {unexpected}, missing {missing}"
        )

    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
