"""Build allowlisted Python source/wheel artifacts and dependency/license inventories."""

import argparse
import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def inventory(output):
    output.mkdir(parents=True, exist_ok=True)
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    components = []
    for item in lock["package"]:
        components.append(
            {
                "type": "library",
                "name": item["name"],
                "version": item["version"],
                "purl": f"pkg:pypi/{item['name']}@{item['version']}",
                "properties": [
                    {"name": "voice:locked-source", "value": json.dumps(item["source"])}
                ],
            }
        )
    npm = json.loads((ROOT / "package-lock.json").read_text())
    for key, item in npm["packages"].items():
        if not key or not item.get("version"):
            continue
        name = item.get("name", key.rsplit("node_modules/", 1)[-1])
        components.append(
            {
                "type": "library",
                "name": name,
                "version": item["version"],
                "purl": f"pkg:npm/{name}@{item['version']}",
                "properties": [{"name": "voice:npm-integrity", "value": item.get("integrity", "")}],
            }
        )
    cargo = tomllib.loads((ROOT / "apps/desktop/src-tauri/Cargo.lock").read_text())
    for item in cargo["package"]:
        components.append(
            {
                "type": "library",
                "name": item["name"],
                "version": item["version"],
                "purl": f"pkg:cargo/{item['name']}@{item['version']}",
                **(
                    {"hashes": [{"alg": "SHA-256", "content": item["checksum"]}]}
                    if item.get("checksum")
                    else {}
                ),
            }
        )
    (output / "sbom.cdx.json").write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.5",
                "version": 1,
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "voice-studio",
                        "version": npm["version"],
                    }
                },
                "components": components,
            },
            indent=2,
        )
        + "\n"
    )
    notices = [
        "Voice Studio dependency notices\n\n"
        "This inventory covers installed Python distributions and locked npm metadata. "
        "It is not a legal license audit. Review upstream obligations and distribute "
        "FFmpeg build licenses/corresponding source separately with each native binary.\n"
    ]
    for distribution in sorted(
        importlib.metadata.distributions(), key=lambda d: d.metadata.get("Name", "")
    ):
        name = distribution.metadata.get("Name", "unknown")
        license_text = (
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or "Unspecified; inspect upstream"
        )
        notices.append(f"\n{name} {distribution.version}\nLicense: {license_text}\n")
        for file in distribution.files or []:
            lower = str(file).lower()
            if ".dist-info/" in lower and any(x in lower for x in ("license", "copying", "notice")):
                path = distribution.locate_file(file)
                if path.is_file():
                    notices.append(path.read_text(errors="replace") + "\n")
    for key, item in npm["packages"].items():
        if key:
            notices.append(
                f"\nnpm {key}: {item.get('version', '')}; "
                f"license: {item.get('license', 'unspecified')}\n"
            )
    (output / "THIRD_PARTY_NOTICES.txt").write_text("".join(notices))
    for path in ("LICENSE", "uv.lock", "package-lock.json"):
        shutil.copy2(ROOT / path, output / path)
    shutil.copy2(ROOT / "apps/desktop/src-tauri/Cargo.lock", output / "Cargo.lock")
    shutil.copy2(
        ROOT / "python/core/src/voice_studio_core/model-lock.json", output / "model-lock.json"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/sources")
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    inventory(args.output)
    if not args.inventory_only:
        for name in ("core", "engine", "service", "cli"):
            subprocess.run(
                [
                    args.uv,
                    "build",
                    "--package",
                    f"voice-studio-{name}",
                    "--out-dir",
                    str(args.output.resolve()),
                ],
                cwd=ROOT,
                check=True,
            )
        # Source archive contains tracked, reviewed files only; no local audio/cache/credentials.
        subprocess.run(
            [
                "git",
                "archive",
                "--format=tar.gz",
                f"--output={args.output.resolve() / 'voice-studio-source.tar.gz'}",
                "HEAD",
            ],
            cwd=ROOT,
            check=True,
        )
    hashes = {}
    for path in sorted(args.output.iterdir()):
        if path.is_file() and path.name != "SHA256SUMS.json":
            with path.open("rb") as stream:
                hashes[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    (args.output / "SHA256SUMS.json").write_text(json.dumps(hashes, indent=2) + "\n")


if __name__ == "__main__":
    main()
