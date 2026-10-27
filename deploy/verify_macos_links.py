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


def version_tuple(value):
    return tuple(int(part) for part in value.split(".")) + (0,) * (3 - len(value.split(".")))


def verify(bundle: Path, minimum_macos=None, repair_rpaths=False):
    count, external, repaired = 0, [], []
    required = (0, 0, 0)
    for path in bundle.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as source:
            if source.read(4) not in MAGIC:
                continue
        count += 1
        output = subprocess.run(
            ["otool", "-l", str(path)], check=True, capture_output=True, text=True
        ).stdout
        command = ""
        dependencies = []
        rpaths = []
        for line in output.splitlines():
            value = line.strip()
            if value.startswith("cmd "):
                command = value.split()[1]
            elif (command == "LC_BUILD_VERSION" and value.startswith("minos ")) or (
                command == "LC_VERSION_MIN_MACOSX" and value.startswith("version ")
            ):
                required = max(required, version_tuple(value.split()[1]))
            elif command in {
                "LC_LOAD_DYLIB",
                "LC_LOAD_WEAK_DYLIB",
                "LC_REEXPORT_DYLIB",
                "LC_LAZY_LOAD_DYLIB",
                "LC_LOAD_UPWARD_DYLIB",
                "LC_RPATH",
            }:
                field = "path " if command == "LC_RPATH" else "name "
                if value.startswith(field):
                    dependency = value[len(field) :].split(" (offset", 1)[0]
                    if command == "LC_RPATH" and dependency.startswith("/") and not dependency.startswith(
                        ("/System/Library/", "/usr/lib/")
                    ) and repair_rpaths:
                        rpaths.append(dependency)
                    else:
                        dependencies.append(dependency)
        if rpaths:
            for dependency in set(rpaths):
                subprocess.run(["install_name_tool", "-delete_rpath", dependency, str(path)], check=True,
                               capture_output=True)
                repaired.append({"binary": str(path.relative_to(bundle)), "removed": dependency})
            # Changing load commands invalidates the build-time ad-hoc signature.
            subprocess.run(["codesign", "--force", "--sign", "-", str(path)], check=True,
                           capture_output=True)
        for dependency in dependencies:
            if dependency.startswith("/") and not dependency.startswith(
                ("/System/Library/", "/usr/lib/")
            ):
                external.append({"binary": str(path.relative_to(bundle)), "dependency": dependency})
    if not count:
        raise ValueError("No Mach-O binaries found")
    if external:
        raise ValueError("External development library references: " + json.dumps(external))
    if minimum_macos and required > version_tuple(minimum_macos):
        raise ValueError(f"Runtime requires macOS {required}; advertised minimum is {minimum_macos}")
    return {"macho_files": count, "external_development_libraries": False,
            "minimum_macos": ".".join(map(str, required)), "repaired_rpaths": repaired}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--minimum-macos")
    parser.add_argument("--repair-rpaths", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.bundle.resolve(), args.minimum_macos, args.repair_rpaths)))


if __name__ == "__main__":
    main()
