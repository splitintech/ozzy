"""Build a native, directory-based runtime. Never copy user data or model weights."""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
}
PRIVATE_SUFFIXES = {
    ".wav",
    ".mp3",
    ".pcm",
    ".flac",
    ".ogg",
    ".m4a",
    ".safetensors",
    ".ckpt",
    ".pt",
    ".pth",
    ".pkl",
    ".pickle",
    ".p12",
    ".pfx",
    ".key",
    ".sqlite",
    ".sqlite3",
}


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_metadata(metadata):
    expected = "voice/voice.exe" if metadata["platform"].startswith("x86_64-pc") else "voice/voice"
    url = urlsplit(metadata["url"])
    if (
        metadata["schema"] != 1
        or metadata["platform"] not in PLATFORMS.values()
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", metadata["version"])
        or type(metadata["sequence"]) is not int
        or metadata["sequence"] < 1
        or metadata["executable"] != expected
        or not 0 < metadata["bytes"] <= 8_000_000_000
        or not metadata["bytes"] <= metadata["unpacked_bytes"] <= 16_000_000_000
        or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])
        or url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.fragment
    ):
        raise ValueError("Runtime metadata violates the desktop installer contract")


def archive_metadata(archive, version, sequence, target, url):
    """Inspect every ZIP member; archives are bounded and contain no symbolic links."""
    seen, total = set(), 0
    executable = "voice/voice.exe" if target == "x86_64-pc-windows-msvc" else "voice/voice"
    with zipfile.ZipFile(archive) as pack:
        if len(pack.infolist()) > 100_000:
            raise ValueError("Too many runtime files")
        for item in pack.infolist():
            path = Path(item.filename)
            key = item.filename.casefold().rstrip("/")
            if (
                path.is_absolute()
                or ".." in path.parts
                or "." in path.parts
                or "\\" in item.filename
                or ":" in item.filename
                or not item.filename.startswith("voice/")
                or key in seen
                or stat.S_ISLNK(item.external_attr >> 16)
                or path.suffix.lower() in PRIVATE_SUFFIXES
                or any(part in {".env", ".download-cache"} for part in path.parts)
                or item.filename.startswith(("voice/data/", "voice/models/"))
            ):
                raise ValueError(f"Forbidden runtime member: {item.filename}")
            seen.add(key)
            total += item.file_size
        if "voice/development_only_not_redistributable.txt" in seen:
            raise ValueError("Development bundles cannot be distributed")
        if executable not in seen:
            raise ValueError("Runtime executable is missing")
    metadata = {
        "schema": 1,
        "version": version,
        "sequence": sequence,
        "platform": target,
        "url": url,
        "bytes": archive.stat().st_size,
        "unpacked_bytes": total,
        "sha256": digest(archive),
        "executable": executable,
    }
    validate_metadata(metadata)
    return metadata


def pack_runtime(bundle, output, version, sequence, target, url):
    """Dereference only links contained in our own PyInstaller output."""
    bundle, output = bundle.resolve(), output.resolve()
    if bundle.name != "voice" or not bundle.is_dir():
        raise ValueError("Expected PyInstaller's voice directory")
    if output.is_relative_to(bundle):
        raise ValueError("Archive output must be outside the runtime bundle")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"voice-runtime-{version}-{target}.zip"
    seen = set()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as pack:
        # os.walk follows framework links; realpath ancestry prevents cycles/escapes.
        def visit(directory, logical, ancestors):
            real = directory.resolve(strict=True)
            if not real.is_relative_to(bundle) or real in ancestors:
                raise ValueError(f"Unsafe or cyclic runtime link: {logical}")
            for child in sorted(directory.iterdir()):
                name = logical / child.name
                resolved = child.resolve(strict=True)
                if not resolved.is_relative_to(bundle):
                    raise ValueError(f"Runtime link escapes build output: {name}")
                if child.is_dir():
                    visit(child, name, ancestors | {real})
                elif child.is_file():
                    if name.as_posix().casefold() in seen:
                        raise ValueError(f"Duplicate runtime member: {name}")
                    seen.add(name.as_posix().casefold())
                    # ZipFile.write follows the symlink and records the regular-file mode.
                    pack.write(resolved, name.as_posix())
                else:
                    raise ValueError(f"Non-regular runtime member: {name}")

        visit(bundle, Path("voice"), set())
    metadata = archive_metadata(archive, version, sequence, target, url)
    manifest = archive.with_suffix(".manifest.json")
    manifest.write_text(json.dumps(metadata, indent=2) + "\n")
    return archive, manifest


def build(args):
    target = PLATFORMS.get((platform.system(), platform.machine()))
    if target is None:
        raise ValueError("Build natively on Apple Silicon macOS or Windows x64")
    if sys.version_info[:2] != (3, 11):
        raise ValueError("The locked runtime requires Python 3.11")
    suffix = ".exe" if os.name == "nt" else ""
    ffmpeg = args.ffmpeg_dir.resolve()
    for name in ("ffmpeg", "ffprobe"):
        binary = ffmpeg / (name + suffix)
        if not binary.is_file():
            raise ValueError(f"Missing user-provided binary: {binary}")
        subprocess.run([str(binary), "-version"], check=True, capture_output=True)
    notices = [
        p
        for p in ffmpeg.iterdir()
        if p.is_file()
        and any(
            token in p.name.lower() for token in ("license", "copying", "notice", "source-offer")
        )
    ]
    if args.development and not args.no_pack:
        raise ValueError("Development bundles cannot be packed or distributed")
    if not notices and not args.development:
        raise ValueError("FFmpeg distribution must include license/notices and source obligations")
    studio = ROOT / "apps/studio/dist"
    if not (studio / "index.html").is_file():
        raise ValueError("Build the studio first with npm ci && npm run build")
    output = args.output.resolve()
    work = output / "pyinstaller"
    work.mkdir(parents=True, exist_ok=True)
    # Native Tauri builds can rebuild dist concurrently; freeze a private UI snapshot first.
    staged_studio = work / "studio"
    if staged_studio.exists():
        shutil.rmtree(staged_studio)
    shutil.copytree(studio, staged_studio)
    studio = staged_studio
    from voice_studio_service.deployment import bundle_context
    deployment_bundle = work / "deploy-source.tar.gz"
    bundle_context(ROOT, deployment_bundle)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        "voice",
        "--additional-hooks-dir",
        str(ROOT / "deploy/hooks"),
        "--distpath",
        str(output / "native"),
        "--workpath",
        str(work / "work"),
        "--specpath",
        str(work),
        "--add-data",
        f"{deployment_bundle}{os.pathsep}voice_studio_cli",
        "--add-data",
        f"{studio}{os.pathsep}studio",
        "--add-data",
        f"{ROOT / 'LICENSE'}{os.pathsep}notices",
    ]
    for name in ("ffmpeg", "ffprobe"):
        command += ["--add-binary", f"{ffmpeg / (name + suffix)}{os.pathsep}ffmpeg"]
    for notice in notices:
        command += ["--add-data", f"{notice}{os.pathsep}notices/ffmpeg"]
    dependency_notices = ROOT / "dist/sources/THIRD_PARTY_NOTICES.txt"
    if not dependency_notices.is_file() and not args.development:
        raise ValueError(
            "Generate dependency notices before packaging with deploy/build_sources.py"
        )
    if dependency_notices.is_file():
        command += ["--add-data", f"{dependency_notices}{os.pathsep}notices"]
    # importlib.metadata and package data are used dynamically by the inference stack.
    for package in (
        "voice_studio_core",
        "voice_studio_engine",
        "voice_studio_service",
        "voice_studio_cli",
        "chatterbox",
        "perth",
        "s3tokenizer",
        "tokenizers",
        "safetensors",
        "torchaudio",
        "librosa",
        "omegaconf",
        "hydra",
    ):
        command += ["--collect-data", package, "--collect-binaries", package]
    command += ["--collect-data", "transformers"]
    for module in (
        "transformers.models.gpt2.modeling_gpt2",
        "transformers.models.gpt2.tokenization_gpt2",
        "transformers.models.llama.modeling_llama",
        "transformers.models.llama.tokenization_llama",
        "transformers.models.auto.tokenization_auto",
        "transformers.generation.utils",
    ):
        command += ["--hidden-import", module]
    for distribution in (
        "torch",
        "torchaudio",
        "transformers",
        "chatterbox-tts",
        "resemble-perth",
        "huggingface-hub",
        "voice-studio-core",
        "voice-studio-engine",
        "voice-studio-service",
        "voice-studio-cli",
    ):
        command += ["--copy-metadata", distribution]
    command += ["--hidden-import", "keyring.backends.macOS" if sys.platform == "darwin" else "keyring.backends.Windows"]
    command += [
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.lifespan.on",
        "--hidden-import",
        "keyring.backends",
        "--exclude-module",
        "pytest",
        "--exclude-module",
        "IPython",
        str(ROOT / "scripts/runtime_entry.py"),
    ]
    build_env = os.environ.copy()
    build_env.update(
        {
            "HF_HOME": str(work / "hf"),
            "HUGGINGFACE_HUB_CACHE": str(work / "hf/hub"),
            "PYINSTALLER_CONFIG_DIR": str(work / "cache"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "NUMBA_CACHE_DIR": str(work / "numba-cache"),
            "VOICE_MODE": "local",
            "VOICE_DATABASE_URL": "",
            "VOICE_DATA_DIR": str(work / "probe-data"),
        }
    )
    subprocess.run(command, cwd=ROOT, env=build_env, check=True)
    bundle = output / "native/voice"
    if args.development:
        (bundle / "DEVELOPMENT_ONLY_NOT_REDISTRIBUTABLE.txt").write_text(
            "Unsigned local development bundle. FFmpeg distribution obligations are unverified.\n"
        )
    if platform.system() == "Darwin":
        audit = [sys.executable, str(ROOT / "deploy/verify_macos_links.py"), str(bundle),
                 "--repair-rpaths"]
        if not args.development:
            config = json.loads((ROOT / "apps/desktop/src-tauri/tauri.conf.json").read_text())
            audit += ["--minimum-macos", config["bundle"]["macOS"]["minimumSystemVersion"]]
        subprocess.run(
            audit, check=True
        )
    subprocess.run(
        [sys.executable, str(ROOT / "deploy/smoke_runtime.py"),
         str(bundle / ("voice" + suffix)), "--output", str(work / "smoke")],
        env=build_env, check=True
    )
    if not args.no_pack:
        archive, manifest = pack_runtime(
            bundle, output, args.version, args.sequence, target, args.url
        )
        print(json.dumps({"archive": str(archive), "manifest": str(manifest)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    native = commands.add_parser("build")
    native.add_argument("--ffmpeg-dir", type=Path, required=True)
    native.add_argument(
        "--development",
        action="store_true",
        help="Local smoke bundle only; requires --no-pack, never distributable",
    )
    native.add_argument(
        "--no-pack", action="store_true", help="Sign native binaries first, then use pack"
    )
    packing = commands.add_parser("pack")
    packing.add_argument("--bundle", type=Path, required=True)
    packing.add_argument("--platform", choices=list(PLATFORMS.values()), required=True)
    for command in (native, packing):
        command.add_argument("--output", type=Path, default=ROOT / "dist/runtime")
        command.add_argument("--version", required=True)
        command.add_argument("--sequence", type=int, required=True)
        command.add_argument(
            "--url", required=True, help="Direct HTTPS ZIP URL; installer refuses redirects"
        )
    args = parser.parse_args()
    try:
        if args.command == "build":
            build(args)
        else:
            archive, manifest = pack_runtime(
                args.bundle, args.output, args.version, args.sequence, args.platform, args.url
            )
            print(json.dumps({"archive": str(archive), "manifest": str(manifest)}))
    except (ValueError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"Runtime build failed: {exc}\n")


if __name__ == "__main__":
    main()
