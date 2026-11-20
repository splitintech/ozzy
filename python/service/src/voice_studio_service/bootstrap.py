"""Opt-in provider-held startup material, private to the root supervisor.

This is persistent sandbox disk, never an image layer or an application volume.
The application user cannot read it. Owner recovery remains in the OS keychain.
"""

import base64
import json
import os
import stat
import tempfile
from pathlib import Path

from voice_studio_core.contracts import DomainError

PERSISTED_BOOTSTRAP = Path("/var/lib/voice-control/bootstrap.json")
MAX_BYTES = 1024 * 1024
SECRET_FIELDS = ("api_key", "owner_password_hash", "session_secret", "backup_key")


def _invalid():
    return DomainError("bootstrap_invalid", "Private runtime startup configuration is invalid", 503)


def _private_directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise _invalid()


def _read(path):
    _private_directory(path.parent)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise _invalid()
        raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise _invalid()
        payload = json.loads(raw)
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(key), str) or not payload[key] for key in SECRET_FIELDS
    ):
        raise _invalid()
    if len(base64.b64decode(payload["backup_key"], validate=True)) != 32:
        raise _invalid()
    if not isinstance(payload.get("deployment"), dict) or not payload.get("sandbox_id"):
        raise _invalid()
    current = os.getenv("DAYTONA_SANDBOX_ID")
    if current and payload["sandbox_id"] != current:
        raise _invalid()
    # Never persist unrelated owner-side credentials or arbitrary top-level data.
    return {key: payload[key] for key in (*SECRET_FIELDS, "deployment", "sandbox_id")}


def _persist(payload, path):
    _private_directory(path.parent, create=True)
    descriptor, name = tempfile.mkstemp(prefix=".bootstrap-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def load_bootstrap(ram, persistent=None):
    """Consume owner bootstrap once; opted-in cold wakes reuse private disk."""
    persistent = persistent or PERSISTED_BOOTSTRAP
    try:
        incoming = ram.exists()
        payload = _read(ram if incoming else persistent)
        enabled = payload["deployment"].get("on_demand_bootstrap") is True
        if not incoming and not enabled:
            raise _invalid()
        if incoming:
            if enabled:
                _persist(payload, persistent)
            ram.unlink()
        return payload
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise _invalid() from exc
