"""Emit a stable Tauri overlay only after validating release configuration."""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


def main():
    version = os.environ["RELEASE_VERSION"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Stable candidates require a numeric major.minor.patch version")
    for key in ("VOICE_RUNTIME_PUBLIC_KEY", "VOICE_UPDATE_PUBLIC_KEY"):
        if not os.environ.get(key):
            raise ValueError(f"Missing {key}")
    for key in ("VITE_RUNTIME_MANIFEST_URL", "VOICE_UPDATE_URL"):
        url = urlsplit(os.environ.get(key, ""))
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.fragment
        ):
            raise ValueError(f"Configure a public HTTPS {key}")
    document = {"version": version, "bundle": {"createUpdaterArtifacts": True}}
    if os.name == "nt":
        thumbprint = os.environ["WINDOWS_CERTIFICATE_THUMBPRINT"]
        if not re.fullmatch(r"[0-9a-fA-F]{40}", thumbprint):
            raise ValueError("Invalid Windows certificate thumbprint")
        timestamp = os.environ["WINDOWS_TIMESTAMP_URL"]
        if urlsplit(timestamp).scheme != "https":
            raise ValueError("Windows timestamp service must use HTTPS")
        document["bundle"]["windows"] = {
            "certificateThumbprint": thumbprint,
            "digestAlgorithm": "sha256",
            "timestampUrl": timestamp,
            "tsp": True,
        }
    Path("deploy/tauri.candidate.generated.json").write_text(json.dumps(document, indent=2) + "\n")


if __name__ == "__main__":
    main()
