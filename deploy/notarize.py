"""Submit an already signed ZIP/DMG to Apple's notary service; retain the actual response."""

import argparse
import json
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--staple", action="store_true", help="For DMG/app, never a plain ZIP")
    args = parser.parse_args()
    profile = os.environ["APPLE_NOTARY_KEYCHAIN_PROFILE"]
    process = subprocess.run(
        [
            "xcrun",
            "notarytool",
            "submit",
            str(args.artifact),
            "--keychain-profile",
            profile,
            "--wait",
            "--output-format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(process.stdout)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if result.get("status") != "Accepted":
        raise SystemExit("Apple notarization did not accept this artifact")
    if args.staple:
        subprocess.run(["xcrun", "stapler", "staple", str(args.artifact)], check=True)
        subprocess.run(["xcrun", "stapler", "validate", str(args.artifact)], check=True)


if __name__ == "__main__":
    main()
