"""Create/verify the base64 Ed25519 envelope consumed by the Rust installer."""

import argparse
import base64
import json
import os
from pathlib import Path

from build_runtime import archive_metadata, validate_metadata
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def verify(envelope, public_key):
    payload = base64.b64decode(envelope["payload"], validate=True)
    signature = base64.b64decode(envelope["signature"], validate=True)
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(
        signature, payload
    )
    metadata = json.loads(payload)
    validate_metadata(metadata)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    try:
        document = json.loads(args.manifest.read_text())
        public = os.environ["VOICE_RUNTIME_PUBLIC_KEY"]
        if args.verify:
            metadata = verify(document, public)
        else:
            if args.output is None or args.archive is None:
                raise ValueError("Signing requires --archive and --output")
            private = Ed25519PrivateKey.from_private_bytes(
                base64.b64decode(os.environ["VOICE_RUNTIME_PRIVATE_KEY"], validate=True)
            )
            actual_public = base64.b64encode(
                private.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            ).decode()
            if actual_public != public:
                raise ValueError("Signing key does not match VOICE_RUNTIME_PUBLIC_KEY")
            validate_metadata(document)
            payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
            document = {
                "payload": base64.b64encode(payload).decode(),
                "signature": base64.b64encode(private.sign(payload)).decode(),
            }
            metadata = verify(document, public)
        if args.archive:
            actual = archive_metadata(
                args.archive,
                metadata["version"],
                metadata["sequence"],
                metadata["platform"],
                metadata["url"],
            )
            if actual != metadata:
                raise ValueError("Archive does not match signed runtime metadata")
        if not args.verify:
            args.output.write_text(json.dumps(document, indent=2) + "\n")
        print(
            json.dumps(
                {"verified": True, "platform": metadata["platform"], "sha256": metadata["sha256"]}
            )
        )
    except Exception as exc:
        # Do not print secret key material or parser inputs.
        parser.exit(1, f"Runtime signing/verification failed ({type(exc).__name__})\n")


if __name__ == "__main__":
    main()
