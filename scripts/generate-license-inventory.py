#!/usr/bin/env python3
"""Generate reviewable dependency and image-license inputs without network access."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import pathlib
import re
from email.message import Message


ROOT = pathlib.Path(__file__).resolve().parents[1]
IMAGE_RE = re.compile(r"(?:^FROM\s+|\bimage:\s*)([^\s#]+)", re.MULTILINE | re.IGNORECASE)


def metadata_license(metadata: Message) -> str:
    expression = metadata.get("License-Expression")
    if expression:
        return expression
    classifiers = [
        value.removeprefix("License :: ")
        for value in metadata.get_all("Classifier", [])
        if value.startswith("License :: ")
    ]
    return "; ".join(classifiers) or metadata.get("License") or "UNKNOWN"


def python_packages() -> list[dict[str, str]]:
    ignored = {"sandbox-platform", "pip", "setuptools", "wheel"}
    packages = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name", "")
        if not name or name.lower() in ignored:
            continue
        packages.append(
            {
                "name": name,
                "version": distribution.version,
                "license": metadata_license(distribution.metadata),
                "source": "python-environment",
            }
        )
    return sorted(packages, key=lambda item: item["name"].lower())


def npm_packages() -> list[dict[str, str]]:
    lock = json.loads((ROOT / "console/package-lock.json").read_text(encoding="utf-8"))
    packages = []
    for path, data in lock.get("packages", {}).items():
        if not path or "node_modules/" not in path:
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        packages.append(
            {
                "name": name,
                "version": str(data.get("version", "UNKNOWN")),
                "license": str(data.get("license", "UNKNOWN")),
                "source": "console/package-lock.json",
            }
        )
    return sorted(packages, key=lambda item: (item["name"], item["version"]))


def images() -> list[dict[str, str]]:
    references: set[str] = set()
    candidates = list(ROOT.glob("**/Dockerfile"))
    candidates += list((ROOT / "k8s").rglob("*.yaml"))
    candidates += list((ROOT / "overlays").rglob("*.yaml"))
    for path in candidates:
        if any(part in {"node_modules", ".venv"} for part in path.parts):
            continue
        references.update(IMAGE_RE.findall(path.read_text(encoding="utf-8")))
    return [
        {
            "name": reference,
            "version": reference.rsplit(":", 1)[-1] if ":" in reference else "latest",
            "license": "SEE_IMAGE_SBOM",
            "source": "container-manifest",
        }
        for reference in sorted(references)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=pathlib.Path, required=True)
    parser.add_argument("--markdown", type=pathlib.Path, required=True)
    args = parser.parse_args()
    inventory = {
        "format": "sandbox-license-inventory-v1",
        "generated_from": "resolved Python environment, npm lockfile, and image manifests",
        "python": python_packages(),
        "npm": npm_packages(),
        "images": images(),
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rows = [
        "# Generated third-party inventory",
        "",
        "This file is release evidence, not legal advice. Review UNKNOWN entries and image SBOMs.",
        "",
        "| Ecosystem | Component | Version | Declared license |",
        "| --- | --- | --- | --- |",
    ]
    for ecosystem in ("python", "npm", "images"):
        for item in inventory[ecosystem]:
            values = [ecosystem, item["name"], item["version"], item["license"]]
            rows.append("| " + " | ".join(value.replace("|", "\\|") for value in values) + " |")
    args.markdown.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
