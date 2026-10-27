"""Build a clearly labeled macOS preview DMG without Finder automation."""

import argparse
import hashlib
import json
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path


def build(app, output):
    app, output = app.resolve(), output.resolve()
    if app.suffix != ".app" or not app.is_dir():
        raise ValueError("Provide a built preview .app directory")
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if "preview" not in info.get("CFBundleName", "").lower():
        raise ValueError("This helper only builds explicitly named preview applications")
    if output.suffix != ".dmg" or output.exists():
        raise ValueError("Choose a new .dmg output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="preview-dmg-") as temporary:
        stage = Path(temporary)
        shutil.copytree(app, stage / app.name, symlinks=True)
        (stage / "Applications").symlink_to("/Applications", target_is_directory=True)
        subprocess.run(["hdiutil", "create", "-volname", "Voice Studio Preview",
                        "-srcfolder", str(stage), "-format", "UDZO", str(output)], check=True)
    subprocess.run(["hdiutil", "verify", str(output)], check=True)
    with output.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {"artifact": str(output), "sha256": digest, "bytes": output.stat().st_size,
              "channel": "preview", "stable_release_approved": False,
              "os_signature_qualified": False, "clean_machine_qualified": False}
    output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.app, args.output), indent=2))
