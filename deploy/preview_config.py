"""Keep preview installer version aligned with its runtime pack and separate identity."""

import json
import os
import re
from pathlib import Path


def main():
    version = os.environ["VERSION"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-preview(?:\.[0-9]+)?", version):
        raise ValueError("Use a numeric version ending in -preview or -preview.NUMBER")
    directory = Path(__file__).resolve().parent
    document = json.loads((directory / "tauri.preview.conf.json").read_text())
    document["version"] = version
    (directory / "tauri.preview.generated.json").write_text(json.dumps(document, indent=2) + "\n")


if __name__ == "__main__":
    main()
