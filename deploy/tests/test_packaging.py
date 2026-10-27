import base64
import importlib.util
import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from build_runtime import archive_metadata, pack_runtime  # noqa: E402
from release_gate import qualification  # noqa: E402
from sign_runtime import verify  # noqa: E402


def runtime(tmp_path):
    bundle = tmp_path / "native/voice"
    bundle.mkdir(parents=True)
    (bundle / "voice").write_bytes(b"native executable fixture" * 1000)
    (bundle / "voice").chmod(0o755)
    return bundle


def test_pack_and_signature_tampering(tmp_path):
    archive, path = pack_runtime(
        runtime(tmp_path),
        tmp_path / "out",
        "0.1.0",
        1,
        "aarch64-apple-darwin",
        "https://example.org/runtime.zip",
    )
    metadata = json.loads(path.read_text())
    assert metadata["bytes"] == archive.stat().st_size
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    payload = json.dumps(metadata).encode()
    envelope = {
        "payload": base64.b64encode(payload).decode(),
        "signature": base64.b64encode(key.sign(payload)).decode(),
    }
    assert verify(envelope, public) == metadata
    envelope["payload"] = base64.b64encode(payload.replace(b"0.1.0", b"9.9.9")).decode()
    with pytest.raises(Exception):
        verify(envelope, public)


def test_dereferences_internal_link_rejects_external(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Creating Windows symlinks requires elevated privileges")
    bundle = runtime(tmp_path)
    (bundle / "alias").symlink_to("voice")
    archive, _ = pack_runtime(
        bundle,
        tmp_path / "out",
        "0.1.0",
        1,
        "aarch64-apple-darwin",
        "https://example.org/runtime.zip",
    )
    with zipfile.ZipFile(archive) as pack:
        assert not stat.S_ISLNK(pack.getinfo("voice/alias").external_attr >> 16)
        assert pack.read("voice/alias") == pack.read("voice/voice")
    (bundle / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        pack_runtime(
            bundle,
            tmp_path / "out",
            "0.1.0",
            1,
            "aarch64-apple-darwin",
            "https://example.org/runtime.zip",
        )


@pytest.mark.parametrize(
    "member", ["../escape", "voice/../escape", "voice\\voice", "voice/data/take.wav"]
)
def test_rejects_unsafe_or_private_members(tmp_path, member):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as pack:
        pack.writestr("voice/voice", b"x" * 1000)
        pack.writestr(member, b"x")
    with pytest.raises(ValueError, match="Forbidden"):
        archive_metadata(path, "0.1.0", 1, "aarch64-apple-darwin", "https://example.org/r.zip")


def test_qualification_never_accepts_a_plan(tmp_path):
    with pytest.raises(ValueError):
        qualification(
            {
                "schema": 1,
                "version": "0.1.0",
                "commit": "a" * 40,
                "owner_authorized": True,
                "reviewed_at": "",
            },
            tmp_path,
            "0.1.0",
            "a" * 40,
        )


def test_runtime_rejects_model_weights(tmp_path):
    archive = tmp_path / "weights.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as pack:
        pack.writestr("voice/voice", b"x" * 1000)
        pack.writestr("voice/_internal/reference.pkl", b"never deserialize")
    with pytest.raises(ValueError, match="Forbidden"):
        archive_metadata(archive, "0.1.0", 1, "aarch64-apple-darwin", "https://example.org/r.zip")


def test_updater_signature_checks_trusted_comment_and_payload(tmp_path):
    import hashlib

    from release_gate import verify_updater

    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = b"fixture1"
    public = base64.b64encode(
        b"untrusted comment: public\n" + base64.b64encode(b"Ed" + key_id + raw)
    ).decode()
    archive = tmp_path / "updater.zip"
    archive.write_bytes(b"real artifact bytes")
    signed = key.sign(hashlib.blake2b(archive.read_bytes()).digest())
    comment = b"timestamp:1234"
    signature = tmp_path / "updater.sig"

    def write_signature(trusted_comment):
        envelope = b"\n".join(
            (
                b"untrusted comment: signature",
                base64.b64encode(b"ED" + key_id + signed),
                b"trusted comment: " + trusted_comment,
                base64.b64encode(key.sign(signed + comment)),
            )
        )
        signature.write_text(base64.b64encode(envelope).decode())

    write_signature(comment)
    assert verify_updater(archive, signature, public) == raw
    write_signature(b"timestamp:9999")
    with pytest.raises(Exception):
        verify_updater(archive, signature, public)
    write_signature(comment)
    archive.write_bytes(b"tampered artifact")
    with pytest.raises(Exception):
        verify_updater(archive, signature, public)


def test_stable_signatures_require_operator_trusted_updater_key(tmp_path):
    from release_gate import signatures

    with pytest.raises(ValueError, match="trusted configured updater"):
        signatures({}, tmp_path, "0.1.0", "a" * 40, "", "")


def test_ffmpeg_extract_requires_notices_and_discards_archive_paths(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "fetch_ffmpeg", SCRIPTS.parent / "deploy/fetch_ffmpeg.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archive = tmp_path / "ffmpeg.zip"
    with zipfile.ZipFile(archive, "w") as pack:
        pack.writestr("release/bin/ffmpeg", b"ffmpeg")
        pack.writestr("release/bin/ffprobe", b"ffprobe")
    with pytest.raises(ValueError, match="license"):
        module.extract_binaries(archive, tmp_path / "without-notices")
    assert not (tmp_path / "without-notices").exists()
    with zipfile.ZipFile(archive, "a") as pack:
        pack.writestr("release/LICENSE.txt", b"License fixture")
        pack.writestr("release/irrelevant.txt", b"not extracted")
    module.extract_binaries(archive, tmp_path / "verified")
    assert sorted(x.name for x in (tmp_path / "verified").iterdir()) == [
        "NOTICE-0.txt",
        "ffmpeg",
        "ffprobe",
    ]


def test_stable_cli_fails_closed_without_evidence(tmp_path):
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "release_gate.py"),
            "--channel",
            "stable",
            "--version",
            "0.1.0",
            "--commit",
            "a" * 40,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["stable_approved"] is False


def test_development_runtime_cannot_be_packaged(tmp_path):
    bundle = runtime(tmp_path)
    (bundle / "DEVELOPMENT_ONLY_NOT_REDISTRIBUTABLE.txt").write_text("Local smoke only")
    with pytest.raises(ValueError, match="Development bundles"):
        pack_runtime(
            bundle,
            tmp_path / "out",
            "0.1.0",
            1,
            "aarch64-apple-darwin",
            "https://example.org/runtime.zip",
        )


def test_ffmpeg_rejects_traversal_before_writing(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "fetch_ffmpeg", SCRIPTS.parent / "deploy/fetch_ffmpeg.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archive = tmp_path / "ffmpeg.zip"
    with zipfile.ZipFile(archive, "w") as pack:
        pack.writestr("../ffmpeg", b"bad")
        pack.writestr("ffprobe", b"bad")
        pack.writestr("LICENSE", b"fixture")
    with pytest.raises(ValueError, match="Unsafe"):
        module.extract_binaries(archive, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_macos_audit_rejects_runtime_newer_than_advertised_os(tmp_path, monkeypatch):
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location(
        "verify_macos_links", SCRIPTS.parent / "deploy/verify_macos_links.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "binary").write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture")
    load_commands = "cmd LC_BUILD_VERSION\n minos 26.0\ncmd LC_LOAD_DYLIB\n name /usr/lib/libSystem.B.dylib (offset 24)\n"
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=load_commands))
    assert module.verify(tmp_path)["minimum_macos"] == "26.0.0"
    with pytest.raises(ValueError, match="advertised minimum"):
        module.verify(tmp_path, "13.0")


def test_macos_audit_repairs_only_rpaths_and_resigns(tmp_path, monkeypatch):
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location(
        "verify_macos_links", SCRIPTS.parent / "deploy/verify_macos_links.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "binary").write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture")
    calls = []

    def run(command, **_):
        calls.append(command)
        return SimpleNamespace(stdout="cmd LC_RPATH\n path /opt/build-tool/lib (offset 12)\n")

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(ValueError, match="External development"):
        module.verify(tmp_path)
    result = module.verify(tmp_path, repair_rpaths=True)
    assert len(result["repaired_rpaths"]) == 1
    assert any(c[:3] == ["install_name_tool", "-delete_rpath", "/opt/build-tool/lib"] for c in calls)
    assert any(c[:4] == ["codesign", "--force", "--sign", "-"] for c in calls)
