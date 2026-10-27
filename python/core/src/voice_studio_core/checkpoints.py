"""Authenticated immutable checkpoints. A corrupt newest commit never falls back silently."""

import base64
import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, text

from .contracts import DomainError
from .database import Artifact, Job, Voice


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class CheckpointStore:
    def __init__(self, root, key, deployment):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.key = base64.b64decode(key, validate=True)
        if len(self.key) != 32:
            raise ValueError("Invalid checkpoint key")
        self.deployment = deployment

    def seal(self, data, context):
        nonce = os.urandom(12)
        return nonce + AESGCM(self.key).encrypt(nonce, data, context.encode())

    def unseal(self, data, context):
        return AESGCM(self.key).decrypt(data[:12], data[12:], context.encode())

    def commits(self):
        return sorted(self.root.glob("[0-9]*.commit.json"))

    def load(self, path=None):
        commits = self.commits()
        if path is None:
            if not commits:
                return None
            path = commits[-1]
        try:
            envelope = json.loads(Path(path).read_text())
            manifest = envelope["manifest"]
            signature = hmac.new(self.key, canonical(manifest), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(envelope["signature"], signature):
                raise ValueError("Invalid checkpoint signature")
            if manifest["deployment"] != self.deployment or manifest["format"] != 1:
                raise ValueError("Wrong checkpoint deployment/format")
            revision = manifest["revision"]
            if type(revision) is not int or revision < 1:
                raise ValueError("Invalid revision")
            encrypted = (self.root / f"{revision:020d}.dump.aes").read_bytes()
            if hashlib.sha256(encrypted).hexdigest() != manifest["sha256"]:
                raise ValueError("Checkpoint checksum mismatch")
            dump = self.unseal(encrypted, f"{self.deployment}:{revision}")
            return manifest, dump
        except Exception as exc:
            raise DomainError(
                "recovery_degraded",
                "Newest committed checkpoint cannot be verified; automatic fallback is disabled",
                503,
            ) from exc

    def write(self, revision, dump, objects, tombstones):
        name = f"{revision:020d}"
        encrypted = self.seal(dump, f"{self.deployment}:{revision}")
        data_path = self.root / (name + ".dump.aes")
        commit = self.root / (name + ".commit.json")
        if commit.exists():
            raise ValueError("Checkpoint revision already committed")
        data_path.write_bytes(encrypted)
        if data_path.read_bytes() != encrypted:
            raise OSError("Checkpoint readback failed")
        manifest = {
            "format": 1,
            "schema": 2,
            "deployment": self.deployment,
            "revision": revision,
            "created_at": time.time(),
            "sha256": hashlib.sha256(encrypted).hexdigest(),
            "objects": objects,
            "tombstones": tombstones,
        }
        envelope = {
            "manifest": manifest,
            "signature": hmac.new(self.key, canonical(manifest), hashlib.sha256).hexdigest(),
        }
        commit.write_bytes(canonical(envelope))
        self.load(commit)
        # Never remove either of the last two VERIFIED checkpoints.
        commits = self.commits()
        for old in commits[:-2]:
            old.unlink()
            old.with_name(old.name.replace(".commit.json", ".dump.aes")).unlink(missing_ok=True)
        for incomplete in self.root.glob("[0-9]*.dump.aes"):
            number = incomplete.name.split(".")[0]
            if (
                number.isdigit()
                and int(number) < revision
                and not (self.root / (number + ".commit.json")).exists()
            ):
                incomplete.unlink(missing_ok=True)
        return manifest


class DurabilityCoordinator:
    def __init__(self, repo, storage, dump=None):
        self.repo, self.storage = repo, storage
        cfg = repo.settings
        self.store = CheckpointStore(
            cfg.checkpoint_dir, cfg.backup_key.get_secret_value(), cfg.deployment_id
        )
        self.lock = threading.Lock()
        self.blocked = False
        self.recover_required = False
        self.dump = dump or self._dump
        self.last = None

    def _dump(self):
        if self.repo.sqlite:
            raise RuntimeError("Hosted checkpoints require PostgreSQL")
        with tempfile.TemporaryDirectory(dir=self.repo.settings.data_dir) as tmp:
            target = Path(tmp) / "database.dump"
            subprocess.run(
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    "--host=/run/postgresql",
                    "--username=voice_backup",
                    "--file=" + str(target),
                    "voice_studio",
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
            return target.read_bytes()

    def checkpoint(self):
        """Caller owns the publication lock. No SQL transaction spans the dump/upload."""
        if self.recover_required:
            raise DomainError(
                "recovery_required",
                "Restart the studio to reconcile an interrupted mutation",
                503,
                True,
            )
        self.blocked = True
        objects = []
        with self.repo.tx() as session:
            tombstones = {
                "voices": list(session.scalars(select(Voice.id).where(Voice.deleted.is_(True)))),
                "jobs": list(session.scalars(select(Job.id).where(Job.deleted.is_(True)))),
            }
            keys = []
            for voice in session.scalars(select(Voice).where(Voice.deleted.is_(False))):
                keys.extend([voice.original_key, voice.reference_key])
            for job in session.scalars(select(Job).where(Job.deleted.is_(False))):
                if job.expires_at and job.expires_at <= time.time():
                    continue
                keys.append(job.script_key)
                if job.state == "completed":
                    keys.extend(
                        session.scalars(select(Artifact.key).where(Artifact.job_id == job.id))
                    )
        for key in sorted(set(keys)):
            receipt = self.storage.verify(key)
            objects.append({"key": key, "checksum": receipt["checksum"], "size": receipt["size"]})
        with self.repo.engine.begin() as connection:
            revision = connection.execute(
                text("UPDATE durable_state SET revision=revision+1 WHERE id=1 RETURNING revision")
            ).scalar_one()
        manifest = self.store.write(revision, self.dump(), objects, tombstones)
        self.last, self.blocked = manifest, False
        return manifest

    def ensure_available(self):
        if self.recover_required:
            raise DomainError(
                "recovery_required",
                "An interrupted mutation requires a studio restart before further writes",
                503,
                True,
            )
        if self.blocked:
            self.checkpoint()

    def receipt(self, identifier, fingerprint):
        with self.repo.engine.connect() as connection:
            row = connection.execute(
                text("SELECT fingerprint,response,created_at FROM operation_receipts WHERE id=:id"),
                {"id": identifier},
            ).first()
        if row:
            if row[2] <= time.time() - 30 * 86400:
                raise DomainError(
                    "operation_expired", "Use a new operation key; this receipt has expired", 410
                )
            if row[0] != fingerprint:
                raise DomainError(
                    "idempotency_conflict",
                    "This operation key was already used for another request",
                    409,
                )
            return json.loads(self.store.unseal(base64.b64decode(row[1]), identifier))

    def record(self, identifier, fingerprint, response):
        cipher = base64.b64encode(self.store.seal(canonical(response), identifier)).decode()
        with self.repo.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO operation_receipts(id,fingerprint,response,revision,created_at) VALUES(:id,:fingerprint,:response,(SELECT revision+1 FROM durable_state WHERE id=1),:now)"
                ),
                {
                    "id": identifier,
                    "fingerprint": fingerprint,
                    "response": cipher,
                    "now": time.time(),
                },
            )
