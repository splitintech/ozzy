"""Reject external development-library references in a frozen macOS runtime."""

import argparse
import json
import subprocess
from pathlib import Path

MAGIC = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


def verify(bundle: Path):
    count, external = 0, []
    for path in bundle.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as source:
            if source.read(4) not in MAGIC:
                continue
        count += 1
        output = subprocess.run(
            ["otool", "-L", str(path)], check=True, capture_output=True, text=True
        ).stdout
        for line in output.splitlines()[1:]:
            dependency = line.strip().split(" (", 1)[0]
            if dependency.startswith("/") and not dependency.startswith(
                ("/System/Library/", "/usr/lib/")
            ):
                external.append({"binary": str(path.relative_to(bundle)), "dependency": dependency})
    if not count:
        raise ValueError("No Mach-O binaries found")
    if external:
        raise ValueError("External development library references: " + json.dumps(external))
    return {"macho_files": count, "external_development_libraries": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.bundle.resolve())))


if __name__ == "__main__":
    main()
