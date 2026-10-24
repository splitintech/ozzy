"""Fetch an operator-reviewed native FFmpeg ZIP by immutable SHA-256, atomically."""

import argparse
import hashlib
import re
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


def extract_binaries(archive: Path, output: Path):
    """Extract only the two executables and legal notices; never archive-controlled paths."""
    with zipfile.ZipFile(archive) as pack:
        total, names, selected = 0, set(), {}
        notices = []
        if len(pack.infolist()) > 10_000:
            raise ValueError("Too many FFmpeg archive members")
        for member in pack.infolist():
            name = PurePosixPath(member.filename)
            total += member.file_size
            if (
                name.is_absolute()
                or ".." in name.parts
                or "\\" in member.filename
                or ":" in member.filename
                or stat.S_ISLNK(member.external_attr >> 16)
                or member.filename.casefold() in names
                or total > 2_000_000_000
            ):
                raise ValueError("Unsafe FFmpeg archive")
            names.add(member.filename.casefold())
            if member.is_dir():
                continue
            for binary in ("ffmpeg", "ffprobe"):
                if name.name in (binary, binary + ".exe"):
                    if binary in selected:
                        raise ValueError(f"Expected exactly one {binary} binary")
                    selected[binary] = member
            if any(
                t in name.name.lower() for t in ("license", "copying", "notice", "source-offer")
            ):
                if member.file_size > 10_000_000:
                    raise ValueError("FFmpeg legal notice is unexpectedly large")
                notices.append(member)
        if set(selected) != {"ffmpeg", "ffprobe"}:
            raise ValueError("Expected one ffmpeg and one ffprobe binary")
        if not notices:
            raise ValueError("FFmpeg archive must include its license and distribution notices")
        output.mkdir(parents=True, exist_ok=False)
        for member in selected.values():
            path = output / PurePosixPath(member.filename).name
            with pack.open(member) as source, path.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            path.chmod(0o755)
        for index, member in enumerate(notices):
            # Fixed names avoid collisions and platform reserved paths.
            with (
                pack.open(member) as source,
                (output / f"NOTICE-{index}.txt").open("wb") as destination,
            ):
                shutil.copyfileobj(source, destination)


def fetch(url: str, sha256: str, output: Path):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256)
    ):
        raise ValueError("Provide a public HTTPS URL and expected SHA-256")
    if output.exists():
        raise ValueError("Output already exists; select a new versioned directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        archive = Path(temp) / "ffmpeg.zip"
        with urllib.request.urlopen(url, timeout=120) as response, archive.open("wb") as out:
            final = urlsplit(response.url)
            if final.scheme != "https" or final.username or final.password:
                raise ValueError("FFmpeg redirect must remain public HTTPS")
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > 1_000_000_000:
                    raise ValueError("FFmpeg archive exceeds one GB")
                out.write(chunk)
        with archive.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != sha256.lower():
                raise ValueError("FFmpeg archive checksum mismatch")
        staged = Path(temp) / "verified"
        extract_binaries(archive, staged)
        (staged / "NOTICE-provenance.json").write_text(
            __import__("json").dumps({"url": url, "sha256": sha256.lower()}, indent=2) + "\n"
        )
        staged.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        fetch(args.url, args.sha256, args.output)
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        parser.exit(1, f"FFmpeg acquisition failed: {error}\n")


if __name__ == "__main__":
    main()
