"""Immutable, checksummed objects on FUSE; no rename or distributed file locks."""

import hashlib
import json
from pathlib import Path

from .contracts import DomainError
from .storage import safe_key, sha256


class VolumeStorage:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key):
        path = (self.root / safe_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise DomainError("invalid_key", "Object escapes volume", 422)
        return path

    def _marker(self, key):
        return (
            self.root / "receipts" / (hashlib.sha256(safe_key(key).encode()).hexdigest() + ".json")
        )

    def verify(self, key):
        receipt = json.loads(self._marker(key).read_text())
        path = self.path(key)
        if (
            receipt["key"] != key
            or path.stat().st_size != receipt["size"]
            or sha256(path) != receipt["checksum"]
        ):
            raise DomainError(
                "object_integrity", "Stored object is incomplete or corrupt", 503, True
            )
        return receipt

    def put(self, key, source, content_type="application/octet-stream"):
        receipt = {
            "key": safe_key(key),
            "size": Path(source).stat().st_size,
            "checksum": sha256(source),
            "content_type": content_type,
        }
        marker = self._marker(key)
        if marker.exists():
            if self.verify(key) != receipt:
                raise DomainError(
                    "immutable_object", "An existing object cannot be overwritten", 409
                )
            return {k: receipt[k] for k in ("size", "checksum")}
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Only the elected single sandbox writes. An unmarked partial file may be repaired.
        import shutil

        shutil.copyfile(source, target)
        if sha256(target) != receipt["checksum"] or target.stat().st_size != receipt["size"]:
            raise DomainError("object_integrity", "Upload verification failed", 503, True)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(receipt, sort_keys=True))
        self.verify(key)
        return {k: receipt[k] for k in ("size", "checksum")}

    def read(self, key):
        self.verify(key)
        data = self.path(key).read_bytes()
        if hashlib.sha256(data).hexdigest() != self.verify(key)["checksum"]:
            raise DomainError("object_integrity", "Object changed while reading", 503, True)
        return data

    def download(self, key, destination):
        Path(destination).write_bytes(self.read(key))

    def delete(self, key):
        self._marker(key).unlink(missing_ok=True)
        self.path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix):
        prefix = safe_key(prefix).rstrip("/") + "/"
        for marker in (self.root / "receipts").glob("*.json"):
            receipt = json.loads(marker.read_text())
            if receipt["key"].startswith(prefix):
                self.delete(receipt["key"])
        import shutil

        target = self.path(prefix)
        if target.is_dir():
            shutil.rmtree(target)

    def cleanup_orphans(self, referenced, age_seconds=86400):
        """Reap old unreferenced/partial uploads; never race a bounded active attempt."""
        import time

        referenced = set(referenced)
        cutoff = time.time() - age_seconds
        removed = 0
        for namespace in ("voices", "expiring"):
            for path in (self.root / namespace).rglob("*"):
                if not path.is_file() or path.stat().st_mtime > cutoff:
                    continue
                key = path.relative_to(self.root).as_posix()
                if key not in referenced:
                    self.delete(key)
                    removed += 1
        return removed

    def cleanup_backlog(self, referenced):
        referenced = set(referenced)
        return sum(
            1
            for namespace in ("voices", "expiring")
            for path in (self.root / namespace).rglob("*")
            if path.is_file() and path.relative_to(self.root).as_posix() not in referenced
        )
