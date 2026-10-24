"""Fail closed before stable publication; all evidence is bound to exact artifact hashes."""

import argparse
import base64
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from build_runtime import archive_metadata, digest
from check_qualification import evaluate
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sign_runtime import verify

PLATFORMS = {"aarch64-apple-darwin", "x86_64-pc-windows-msvc"}


def verify_updater(archive, signature, public_key):
    """Verify Tauri's base64-wrapped, prehashed Minisign format (including trusted comment)."""
    key_lines = base64.b64decode(public_key, validate=True).decode().splitlines()
    signature_lines = (
        base64.b64decode(signature.read_text().strip(), validate=True).decode().splitlines()
    )
    key = base64.b64decode(key_lines[1], validate=True)
    signed = base64.b64decode(signature_lines[1], validate=True)
    global_signature = base64.b64decode(signature_lines[3], validate=True)
    if (
        len(key) != 42
        or key[:2] not in (b"Ed", b"ED")
        or len(signed) != 74
        or signed[:2] != b"ED"
        or key[2:10] != signed[2:10]
        or not signature_lines[2].startswith("trusted comment: ")
    ):
        raise ValueError("Invalid updater signature format")
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "blake2b").digest()
    verifier = Ed25519PublicKey.from_public_bytes(key[10:])
    verifier.verify(signed[10:], checksum)
    verifier.verify(global_signature, signed[10:] + signature_lines[2][17:].encode())
    return key[10:]


def evidence(root, record):
    path = (root / record["path"]).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Evidence must be a file beneath the evidence directory")
    if digest(path) != record["sha256"]:
        raise ValueError(f"Evidence hash mismatch: {record['path']}")
    return path


def qualification(document, root, version, commit):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Stable qualification requires a numeric major.minor.patch version")
    if document.get("schema") != 1 or document.get("version") != version:
        raise ValueError("Qualification schema/version mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or document.get("commit") != commit:
        raise ValueError("Qualification must identify the exact source commit")
    if document.get("owner_authorized") is not True or not document.get("reviewed_at"):
        raise ValueError("Owner review and authorization are required")
    reviewed = datetime.fromisoformat(document["reviewed_at"].replace("Z", "+00:00"))
    if reviewed.tzinfo is None:
        raise ValueError("Owner review must carry a timezone-aware timestamp")
    expected_lock = (
        Path(__file__).resolve().parents[1] / "python/core/src/voice_studio_core/model-lock.json"
    )
    if digest(evidence(root, document["model_lock"])) != digest(expected_lock):
        raise ValueError("Qualification model lock does not match this source checkout")
    scores = json.loads(evidence(root, document["scores"]).read_text())
    hosted = json.loads(evidence(root, document["hosted"]).read_text())
    failures = evaluate(scores, hosted)
    if failures:
        raise ValueError("; ".join(failures))
    # Readable records, not booleans alone: actual timing/lifecycle/cost proof must be attached.
    for field in ("hosted_run_log", "recovery_log", "cost_report", "model_lock"):
        path = evidence(root, document[field])
        if path.stat().st_size == 0:
            raise ValueError(f"Empty evidence: {field}")
    native = document["native"]
    if set(native) != PLATFORMS:
        raise ValueError("Qualify both Apple Silicon macOS and Windows x64")
    for target, record in native.items():
        run = json.loads(evidence(root, record).read_text())
        required = (
            "install",
            "launch",
            "reference_import",
            "generate",
            "play",
            "export_wav",
            "export_mp3",
            "restart",
            "delete",
            "runtime_rollback",
            "offline_generation",
            "update",
            "signature_verification",
        )
        if run.get("platform") != target or run.get("commit") != commit:
            raise ValueError("Native qualification platform/commit mismatch")
        if any(run.get("checks", {}).get(check) is not True for check in required):
            raise ValueError(f"Incomplete native qualification: {target}")
        evidence(root, run["log"])
        for artifact in ("runtime_archive", "installer"):
            evidence(root, run[artifact])


def signatures(document, root, version, commit, public_key, updater_public_key, qualified=None):
    if not updater_public_key:
        raise ValueError("A trusted configured updater public key is required")
    if document.get("commit") != commit or document.get("version") != version:
        raise ValueError("Signature evidence must identify this source and version")
    if set(document["platforms"]) != PLATFORMS:
        raise ValueError("Signature evidence is required for both native platforms")
    for target, record in document["platforms"].items():
        archive = evidence(root, record["runtime_archive"])
        envelope = json.loads(evidence(root, record["runtime_manifest"]).read_text())
        manifest = verify(envelope, public_key)
        actual = archive_metadata(archive, version, manifest["sequence"], target, manifest["url"])
        if manifest != actual:
            raise ValueError("Signed runtime archive metadata mismatch")
        native = json.loads(evidence(root, record["native_signatures"]).read_text())
        if native.get("platform") != target or native.get("verified") is not True:
            raise ValueError("Native signature verification is missing")
        if native.get("runtime_sha256") != digest(archive):
            raise ValueError("Native evidence is not bound to the runtime ZIP")
        installer = evidence(root, record["installer"])
        if qualified is None:
            raise ValueError("Signature artifacts must be bound to native qualification")
        tested = json.loads(evidence(root, qualified["native"][target]).read_text())
        for name, artifact in (("runtime_archive", archive), ("installer", installer)):
            if digest(evidence(root, tested[name])) != digest(artifact):
                raise ValueError("Signed artifacts differ from clean-machine qualification")
        if native.get("installer_sha256") != digest(installer):
            raise ValueError("Native evidence is not bound to this installer")
        for key in ("runtime_verify_log", "installer_verify_log"):
            evidence(root, native[key])
        if target == "aarch64-apple-darwin":
            notary = json.loads(evidence(root, native["notarization"]).read_text())
            if not notary.get("runtime_accepted") or not notary.get("installer_accepted"):
                raise ValueError("Both runtime and installer need accepted notarization")
            for key in ("runtime_submission", "installer_submission"):
                submission = json.loads(evidence(root, notary[key]).read_text())
                if submission.get("status") != "Accepted" or not submission.get("id"):
                    raise ValueError("Notary service did not accept the artifact")
        elif native.get("authenticode_valid") is not True:
            raise ValueError("Windows Authenticode verification failed")
        # Tauri updater signatures use a distinct key; native/runtime signatures do not replace it.
        updater = json.loads(evidence(root, record["updater"]).read_text())
        if updater.get("verified") is not True or not updater.get("public_key"):
            raise ValueError("Tauri updater signature evidence is missing")
        updater_archive = evidence(root, updater["archive"])
        updater_signature = evidence(root, updater["signature"])
        if updater["public_key"] != updater_public_key:
            raise ValueError("Updater evidence does not match the configured trusted key")
        if native.get("updater_sha256") != digest(updater_archive):
            raise ValueError("Native evidence is not bound to the updater archive")
        updater_key = verify_updater(updater_archive, updater_signature, updater_public_key)
        if updater_key == base64.b64decode(public_key, validate=True):
            raise ValueError("Runtime and updater keys must be independent")
        evidence(root, updater["verification_log"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=("preview", "stable"), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--evidence-dir", type=Path, default=Path("."))
    parser.add_argument("--qualification", type=Path)
    parser.add_argument("--signatures", type=Path)
    parser.add_argument(
        "--qualification-only",
        action="store_true",
        help="Preflight only. Never authorizes stable publication.",
    )
    args = parser.parse_args()
    failures = []
    if args.channel == "preview":
        print(
            json.dumps(
                {
                    "channel": "preview",
                    "stable_approved": False,
                    "label": "UNSIGNED PREVIEW — NOT QUALIFIED FOR PRODUCTION",
                }
            )
        )
        return
    try:
        if args.qualification is None:
            raise ValueError("An actual qualification record is required")
        document = json.loads(args.qualification.read_text())
        qualification(document, args.evidence_dir, args.version, args.commit)
        if not args.qualification_only:
            if args.signatures is None:
                raise ValueError("Native/runtime/updater signature evidence is required")
            signatures(
                json.loads(args.signatures.read_text()),
                args.evidence_dir,
                args.version,
                args.commit,
                os.environ["VOICE_RUNTIME_PUBLIC_KEY"],
                os.environ["VOICE_UPDATE_PUBLIC_KEY"],
                document,
            )
    except Exception as exc:
        failures.append(str(exc))
    approved = not failures and not args.qualification_only
    print(
        json.dumps(
            {
                "channel": "stable",
                "stable_approved": approved,
                "qualification_preflight": not failures,
                "failures": failures,
            },
            indent=2,
        )
    )
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
