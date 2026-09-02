#!/usr/bin/env python3
"""Create deployable release manifests and a complete cross-image inventory."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
from typing import Any

import yaml


# Keys are the release workflow's ``images`` matrix names: they name the
# ``image-<name>.json`` / ``image-<name>.cdx.json`` evidence files this script
# reads. tests/test_release_assets.py generates those files by running the
# workflow's own step, so a drift between the two fails there, not on tag day.
COMPONENT_IMAGES = {
    "runtime": "sandbox-runtime:0.5.0",
    "file-service": "sandbox-file-service:0.3.0",
    "control-plane": "sandbox-control-plane:0.7.0",
    "console": "sandbox-console:0.1.0",
}
IMAGE_NAME = re.compile(r"^ghcr\.io/[a-z0-9][a-z0-9._/-]*$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def load_image_identities(release_dir: pathlib.Path) -> dict[str, str]:
    identities: dict[str, str] = {}
    for component in COMPONENT_IMAGES:
        path = release_dir / f"image-{component}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("component") != component:
            raise ValueError(f"component mismatch in {path}")
        # ``repository`` is the key the workflow's "Record deployable image
        # identity" step writes; ``image`` was read here while the workflow
        # never wrote it, so every release failed on its last job.
        image = str(data.get("repository", ""))
        digest = str(data.get("digest", ""))
        if not IMAGE_NAME.fullmatch(image) or not DIGEST.fullmatch(digest):
            raise ValueError(f"invalid image identity in {path}")
        identities[component] = f"{image}@{digest}"
    return identities


def replace_manifest_images(value: Any, replacements: dict[str, str], counts: dict[str, int]) -> Any:
    if isinstance(value, str):
        if value in replacements:
            counts[value] += 1
            return replacements[value]
        return value
    if isinstance(value, list):
        return [replace_manifest_images(item, replacements, counts) for item in value]
    if isinstance(value, dict):
        original_image = value.get("image")
        replaced = {
            key: replace_manifest_images(item, replacements, counts)
            for key, item in value.items()
        }
        # A registry-consumable release manifest must permit pulling workloads.
        if original_image and replaced.get("imagePullPolicy") == "Never":
            replaced["imagePullPolicy"] = "IfNotPresent"
        return replaced
    return value


def render_manifest(input_path: pathlib.Path, output_path: pathlib.Path, identities: dict[str, str]) -> None:
    replacements = {
        source: identities[component] for component, source in COMPONENT_IMAGES.items()
    }
    counts = {source: 0 for source in replacements}
    documents = [
        replace_manifest_images(document, replacements, counts)
        for document in yaml.safe_load_all(input_path.read_text(encoding="utf-8"))
        if document is not None
    ]
    missing = [source for source, count in counts.items() if count == 0]
    if missing:
        raise ValueError(f"release manifest did not contain expected images: {missing}")
    rendered = yaml.safe_dump_all(documents, sort_keys=False, explicit_start=True)
    if any(source in rendered for source in replacements):
        raise ValueError("release manifest retains local component image references")
    if "imagePullPolicy: Never" in rendered:
        raise ValueError("release manifest retains a local-only image pull policy")
    output_path.write_text(rendered, encoding="utf-8")


def license_text(component: dict[str, Any]) -> str:
    values: list[str] = []
    for entry in component.get("licenses") or []:
        if "expression" in entry:
            values.append(str(entry["expression"]))
            continue
        license_data = entry.get("license", {})
        value = license_data.get("id") or license_data.get("name")
        if value:
            values.append(str(value))
    return "; ".join(sorted(set(values))) or "UNKNOWN"


def merge_image_sboms(release_dir: pathlib.Path) -> None:
    inventory_path = release_dir / "licenses.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    image_components: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for component in COMPONENT_IMAGES:
        sbom_path = release_dir / f"image-{component}.cdx.json"
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        components = sbom.get("components") or []
        if sbom.get("bomFormat") != "CycloneDX" or not components:
            raise ValueError(f"missing CycloneDX components in {sbom_path}")
        for item in components:
            name = str(item.get("name", ""))
            if not name:
                continue
            version = str(item.get("version", "UNKNOWN"))
            purl = str(item.get("purl", ""))
            key = (component, name, version, purl)
            if key in seen:
                continue
            seen.add(key)
            image_components.append(
                {
                    "image": component,
                    "name": name,
                    "version": version,
                    "type": str(item.get("type", "UNKNOWN")),
                    "license": license_text(item),
                    "purl": purl,
                    "source": sbom_path.name,
                }
            )
    inventory["format"] = "sandbox-license-inventory-v2"
    inventory["generated_from"] = (
        "resolved SDK environment, npm lockfile, image references, and four image CycloneDX SBOMs"
    )
    inventory["image_components"] = sorted(
        image_components,
        key=lambda item: (item["image"], item["name"].lower(), item["version"]),
    )
    inventory_path.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    rows = [
        "# Generated third-party inventory",
        "",
        "This file is release evidence, not legal advice. Review UNKNOWN entries before publishing.",
        "",
        "| Ecosystem | Component | Version | Declared license | Source |",
        "| --- | --- | --- | --- | --- |",
    ]
    for ecosystem in ("python", "npm", "images"):
        for item in inventory.get(ecosystem, []):
            values = [
                ecosystem,
                str(item["name"]),
                str(item["version"]),
                str(item["license"]),
                str(item["source"]),
            ]
            rows.append("| " + " | ".join(value.replace("|", "\\|") for value in values) + " |")
    for item in inventory["image_components"]:
        values = [
            f"image:{item['image']}",
            item["name"],
            item["version"],
            item["license"],
            item["source"],
        ]
        rows.append("| " + " | ".join(value.replace("|", "\\|") for value in values) + " |")
    (release_dir / "licenses.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=pathlib.Path, required=True)
    parser.add_argument("--manifest-input", type=pathlib.Path, required=True)
    parser.add_argument("--manifest-output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    identities = load_image_identities(args.release_dir)
    render_manifest(args.manifest_input, args.manifest_output, identities)
    merge_image_sboms(args.release_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
