"""Sign/verify native runtime files using an already provisioned platform certificate."""

import argparse
import json
import os
import platform
import subprocess
from pathlib import Path


def run(command):
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return result.stdout + result.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    root = args.bundle.resolve()
    logs = []
    if platform.system() == "Darwin":
        identity = os.environ.get("APPLE_SIGNING_IDENTITY", "")
        if not args.verify_only and not identity:
            parser.error("APPLE_SIGNING_IDENTITY is required; import its certificate first")
        binaries = []
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_file() and not path.is_symlink():
                if "Mach-O" in run(["file", "-b", str(path)]):
                    binaries.append(path)
        if not binaries:
            parser.error("No native Mach-O files found")
        entitlements = Path(__file__).with_name("runtime.entitlements.plist")
        for path in binaries:
            if not args.verify_only:
                run(
                    [
                        "codesign",
                        "--force",
                        "--timestamp",
                        "--options",
                        "runtime",
                        "--entitlements",
                        str(entitlements),
                        "--sign",
                        identity,
                        str(path),
                    ]
                )
            logs.append(
                str(path.relative_to(root))
                + "\n"
                + run(["codesign", "--verify", "--strict", "--verbose=2", str(path)])
            )
        # Verify a real Developer ID signature, not an ad-hoc preview signature.
        detail = run(["codesign", "-d", "--verbose=4", str(root / "voice")])
        if "Authority=Developer ID Application:" not in detail or "TeamIdentifier=" not in detail:
            raise ValueError("Runtime is not Developer ID signed")
        logs.append(detail)
    elif platform.system() == "Windows":
        tool = os.environ.get("SIGNTOOL_PATH", "signtool.exe")
        thumbprint = os.environ.get("WINDOWS_CERTIFICATE_THUMBPRINT", "")
        timestamp = os.environ.get("WINDOWS_TIMESTAMP_URL", "")
        if not args.verify_only and (not thumbprint or not timestamp.startswith("https://")):
            parser.error("Provision certificate thumbprint and HTTPS timestamp service")
        binaries = [p for p in root.rglob("*") if p.suffix.lower() in (".exe", ".dll", ".pyd")]
        if not binaries:
            parser.error("No Windows native files found")
        for path in binaries:
            if not args.verify_only:
                run(
                    [
                        tool,
                        "sign",
                        "/sha1",
                        thumbprint,
                        "/fd",
                        "SHA256",
                        "/tr",
                        timestamp,
                        "/td",
                        "SHA256",
                        str(path),
                    ]
                )
            logs.append(
                str(path.relative_to(root))
                + "\n"
                + run([tool, "verify", "/pa", "/all", "/v", str(path)])
            )
    else:
        parser.error("Native signing requires macOS or Windows")
    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.log.write_text("\n".join(logs))
    print(json.dumps({"verified": True, "native_files": len(binaries), "log": str(args.log)}))


if __name__ == "__main__":
    main()
