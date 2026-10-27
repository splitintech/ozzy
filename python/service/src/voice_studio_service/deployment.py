"""Owner-side, resumable single-sandbox provisioning. No audio enters a build context."""

import base64
import hashlib
import io
import json
import os
import secrets
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from daytona import (
    CreateSandboxFromSnapshotParams,
    CreateSnapshotParams,
    Daytona,
    DaytonaConfig,
    Image,
    ListSandboxesQuery,
    Resources,
    VolumeMount,
)
from daytona.common.process import SessionExecuteRequest
from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field
from voice_studio_core.contracts import DomainError

from .cloud import missing, state_name

PROFILE = "hosted_daytona_single"
KEY_SERVICE = "studio.voicestudio.deployments"
ALLOWED_ROOT_FILES = {
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "uv.lock",
    "LICENSE",
    ".npmrc",
}
ALLOWED_SUFFIXES = {
    ".py",
    ".toml",
    ".json",
    ".lock",
    ".ts",
    ".tsx",
    ".js",
    ".css",
    ".html",
    ".svg",
    ".sh",
    ".md",
    ".txt",
}
EXCLUDED_PARTS = {
    "node_modules",
    "target",
    "dist",
    "data",
    "models",
    "artifacts",
    "__pycache__",
    ".git",
    ".venv",
    ".pytest_cache",
    ".download-cache",
    ".env",
}


class DeploymentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    deployment: str = Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")
    profile: Literal["hosted_daytona_single"] = PROFILE
    account: str = ""
    region: str | None = None
    source_sha256: str = ""
    snapshot: str = ""
    snapshot_id: str | None = None
    source_inventory: dict[str, str] = Field(default_factory=dict)
    previous_snapshot: str | None = None
    previous_source_sha256: str | None = None
    previous_source_inventory: dict[str, str] = Field(default_factory=dict)
    update_phase: str | None = None
    candidate_snapshot: str | None = None
    sandbox_id: str | None = None
    objects_volume: str = ""
    checkpoints_volume: str = ""
    architecture: Literal["linux/amd64"] = "linux/amd64"
    cpu: Literal[4] = 4
    memory: Literal[8] = 8
    disk: Literal[20] = 20
    idle_seconds: int = 600
    retention_days: int = 30
    application_version: str = "0.1.0"
    database_schema: int = 2
    checkpoint_format: Literal[1] = 1
    model_revision: str = ""
    initialized: bool = False
    phase: str = "planned"
    credential_ref: str = ""
    key_fingerprint: str = ""


def allowed_source(name):
    from importlib.resources import files

    approved = json.loads(
        files("voice_studio_service").joinpath("build-allowlist.json").read_text()
    )
    return name in approved


def source_context(source, destination):
    source, destination = Path(source).resolve(), Path(destination)
    inventory = {}
    from importlib.resources import files

    approved = json.loads(
        files("voice_studio_service").joinpath("build-allowlist.json").read_text()
    )
    for name in sorted(approved):
        path = source / name
        if not path.is_file():
            raise ValueError("Approved source is missing: " + name)
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError("Build source symlinks are forbidden")
        data = path.read_bytes()
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        inventory[name] = hashlib.sha256(data).hexdigest()
    for required in ("deploy/Dockerfile", "uv.lock", "package-lock.json", "pyproject.toml"):
        if required not in inventory:
            raise ValueError("Incomplete approved source context: " + required)
    digest = hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()
    shutil.copyfile(destination / "deploy/Dockerfile", destination / "Dockerfile")
    return digest, inventory


def bundle_context(source, output):
    with tempfile.TemporaryDirectory() as tmp:
        digest, inventory = source_context(source, tmp)
        with tarfile.open(output, "w:gz") as archive:
            for name in [*inventory, "Dockerfile"]:
                info = archive.gettarinfo(str(Path(tmp) / name), arcname=name)
                info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
                with (Path(tmp) / name).open("rb") as stream:
                    archive.addfile(info, stream)
        return digest


def unpack_context(bundle, destination):
    with (
        tarfile.open(fileobj=bundle) if hasattr(bundle, "read") else tarfile.open(bundle) as archive
    ):
        total = 0
        for member in archive:
            total += member.size
            if (
                total > 100_000_000
                or not member.isfile()
                or not (allowed_source(member.name) or member.name == "Dockerfile")
            ):
                raise ValueError("Invalid packaged build source")
            target = Path(destination) / member.name
            if target.exists():
                raise ValueError("Duplicate build source")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())


class Vault:
    def get(self, deployment):
        import keyring

        value = keyring.get_password(KEY_SERVICE, deployment)
        return json.loads(value) if value else None

    def put(self, deployment, value):
        import keyring

        keyring.set_password(KEY_SERVICE, deployment, json.dumps(value))


class DeploymentManager:
    def __init__(self, root=None, vault=None, sdk=None, progress=None):
        self.root = Path(root or Path.home() / ".voice-studio/deployments")
        self.vault = vault or Vault()
        self.sdk = sdk
        self.progress = progress or (lambda phase: None)

    def path(self, name):
        DeploymentManifest(deployment=name)
        return self.root / (name + ".json")

    def load(self, name):
        return DeploymentManifest.model_validate_json(self.path(name).read_text())

    def exists(self, name):
        return self.path(name).is_file()

    def save(self, manifest, phase=None):
        if phase:
            manifest.phase = phase
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path(manifest.deployment)
        temporary = path.with_suffix(".pending")
        temporary.write_text(manifest.model_dump_json(indent=2))
        temporary.chmod(0o600)
        os.replace(temporary, path)
        self.progress(manifest.phase)

    def connect(self, name, api_key=None):
        credential = self.vault.get(name)
        if not credential:
            raise DomainError("setup_required", "Run hosted setup on this device first", 409)
        key = api_key or credential["api_key"]
        return self.sdk or Daytona(
            DaytonaConfig(api_key=key, target=self.load(name).region, otel_enabled=False)
        )

    def preflight(self, sdk, manifest):
        from daytona_api_client.api.api_keys_api import ApiKeysApi
        from daytona_api_client.api.organizations_api import OrganizationsApi

        api = sdk._api_client  # pinned SDK 0.225.0; covered by compatibility tests
        identity = ApiKeysApi(api).get_current_api_key()
        if not identity.organization_id:
            raise DomainError(
                "account_required", "An organization-scoped Daytona API key is required", 400
            )
        organizations = OrganizationsApi(api)
        organization = organizations.get_organization(identity.organization_id)
        if manifest.account and manifest.account != identity.organization_id:
            raise DomainError(
                "account_mismatch", "This deployment belongs to another Daytona account", 409
            )
        manifest.account = identity.organization_id
        manifest.region = manifest.region or organization.default_region_id
        usage = organizations.get_organization_usage_overview(identity.organization_id)
        region = next(
            (
                r
                for r in usage.region_usage
                if r.region_id == manifest.region
                and str(getattr(r.sandbox_class, "value", r.sandbox_class)) == "container"
            ),
            None,
        )
        if region is None:
            raise DomainError(
                "quota_unknown", "Container quota for this region could not be verified", 503
            )
        for field in ("cpu", "memory", "disk"):
            requested = getattr(manifest, field)
            maximum = getattr(region, f"max_{field}_per_sandbox") or getattr(
                organization, f"max_{field}_per_sandbox"
            )
            available = getattr(region, f"total_{field}_quota") - getattr(
                region, f"current_{field}_usage"
            )
            if field == "disk" and region.max_disk_per_non_ephemeral_sandbox is not None:
                maximum = min(maximum, region.max_disk_per_non_ephemeral_sandbox)
            if requested > maximum or (not manifest.sandbox_id and requested > available):
                raise DomainError(
                    "quota_insufficient",
                    f"This deployment requires {requested} {field}; account quota is insufficient",
                    409,
                )
        missing_volumes = int(not manifest.objects_volume) + int(not manifest.checkpoints_volume)
        if usage.current_volume_usage + missing_volumes > usage.total_volume_quota:
            raise DomainError("quota_insufficient", "Two persistent volumes are required", 409)

    def setup(
        self, name, source=None, api_key=None, owner_password=None, region=None, dry_run=False
    ):
        manifest = (
            self.load(name)
            if self.exists(name)
            else DeploymentManifest(
                deployment=name, region=region, credential_ref=f"{KEY_SERVICE}/{name}"
            )
        )
        with tempfile.TemporaryDirectory() as context:
            if source:
                digest, inventory = source_context(source, context)
            else:
                from importlib.resources import files

                bundled = files("voice_studio_cli").joinpath("deploy-source.tar.gz")
                if not bundled.is_file():
                    raise DomainError(
                        "source_required",
                        "Provide --source PATH or install a release containing approved build sources",
                        400,
                    )
                unpack_context(io.BytesIO(bundled.read_bytes()), context)
                with tempfile.TemporaryDirectory() as validated:
                    digest, inventory = source_context(context, validated)
            if manifest.source_sha256 and manifest.source_sha256 != digest:
                raise DomainError(
                    "update_required",
                    "Source differs from this deployment. Use cloud update explicitly.",
                    409,
                )
            manifest.source_sha256 = digest
            manifest.source_inventory = inventory
            manifest.snapshot = manifest.snapshot or f"voice-single-{digest[:24]}"
            lock = json.loads(
                (Path(context) / "python/core/src/voice_studio_core/model-lock.json").read_text()
            )
            manifest.model_revision = lock["models"]["nano"]["revision"]
            if dry_run:
                return {
                    "dry_run": True,
                    "manifest": manifest.model_dump(),
                    "build": "remote",
                    "provisioned": False,
                }
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with FileLock(str(self.path(name)) + ".lock", timeout=0):
                saved = self.vault.get(name)
                if saved is None:
                    if (
                        manifest.initialized
                        or manifest.objects_volume
                        or manifest.checkpoints_volume
                    ):
                        raise DomainError(
                            "recovery_keys_required",
                            "Import this deployment's recovery bundle; existing keys must not be replaced",
                            409,
                        )
                    if not api_key or not owner_password or len(owner_password) < 12:
                        raise DomainError(
                            "credentials_required",
                            "Provide a Daytona key and an owner password of at least twelve characters",
                            400,
                        )
                    sdk = self.sdk or Daytona(
                        DaytonaConfig(api_key=api_key, target=manifest.region, otel_enabled=False)
                    )
                    self.preflight(sdk, manifest)
                    if list(
                        sdk.list(
                            ListSandboxesQuery(labels={"app": "voice-studio", "deployment": name})
                        )
                    ):
                        raise DomainError(
                            "recovery_keys_required",
                            "An existing deployment requires its recovery bundle",
                            409,
                        )
                    for suffix in ("objects", "checkpoints"):
                        try:
                            sdk.volume.get(f"voice-{name}-{suffix}", create=False)
                        except Exception as exc:
                            if not missing(exc):
                                raise
                        else:
                            raise DomainError(
                                "recovery_keys_required",
                                "Existing storage requires its recovery bundle",
                                409,
                            )
                    from .auth import passwords

                    saved = {
                        "api_key": api_key,
                        "owner_password_hash": passwords.hash(owner_password),
                        "session_secret": secrets.token_urlsafe(48),
                        "backup_key": base64.b64encode(os.urandom(32)).decode(),
                    }
                    self.vault.put(name, saved)
                fingerprint = hashlib.sha256(saved["backup_key"].encode()).hexdigest()
                if manifest.key_fingerprint and manifest.key_fingerprint != fingerprint:
                    raise DomainError(
                        "recovery_keys_required",
                        "The saved recovery keys do not match this deployment",
                        409,
                    )
                manifest.key_fingerprint = fingerprint
                self.save(manifest, "preflight")
                sdk = self.connect(name)
                self.preflight(sdk, manifest)
                self.save(manifest, "building")
                try:
                    snapshot = sdk.snapshot.get(manifest.snapshot)
                except Exception as exc:
                    if not missing(exc):
                        raise
                    from daytona_api_client.api.organizations_api import OrganizationsApi

                    quota = OrganizationsApi(sdk._api_client).get_organization_usage_overview(
                        manifest.account
                    )
                    if quota.current_snapshot_usage >= quota.total_snapshot_quota:
                        raise DomainError(
                            "quota_insufficient", "Snapshot quota is insufficient", 409
                        )
                    image = Image.from_dockerfile(Path(context) / "Dockerfile", strict_context=True)
                    snapshot = sdk.snapshot.create(
                        CreateSnapshotParams(
                            name=manifest.snapshot,
                            image=image,
                            resources=Resources(
                                cpu=manifest.cpu, memory=manifest.memory, disk=manifest.disk
                            ),
                            region_id=manifest.region,
                        ),
                        timeout=1800,
                    )
                deadline = time.monotonic() + 1800
                while state_name(snapshot) != "active":
                    if state_name(snapshot) in {"error", "failed"} or time.monotonic() > deadline:
                        raise DomainError(
                            "build_failed",
                            "Snapshot is not ready; inspect the remote build and rerun setup",
                            503,
                        )
                    time.sleep(2)
                    snapshot = sdk.snapshot.get(manifest.snapshot)
                self.validate_snapshot(snapshot)
                if manifest.snapshot_id and manifest.snapshot_id != snapshot.id:
                    raise DomainError(
                        "snapshot_identity",
                        "Snapshot identity changed; explicit reconciliation is required",
                        409,
                    )
                manifest.snapshot_id = snapshot.id
                for name_field, suffix in (
                    ("objects_volume", "objects"),
                    ("checkpoints_volume", "checkpoints"),
                ):
                    if not getattr(manifest, name_field):
                        volume = sdk.volume.get(f"voice-{name}-{suffix}", create=True)
                        setattr(manifest, name_field, volume.id)
                        self.save(manifest, "volumes")
                    else:
                        volume = sdk.volume.get(f"voice-{name}-{suffix}")
                    deadline = time.monotonic() + 180
                    while state_name(volume) != "ready":
                        if (
                            state_name(volume) in {"error", "deleted", "deleting", "pending_delete"}
                            or time.monotonic() >= deadline
                        ):
                            raise DomainError(
                                "volume_unavailable",
                                "Persistent volume is not ready; retry setup without replacing it",
                                503,
                            )
                        time.sleep(2)
                        volume = sdk.volume.get(f"voice-{name}-{suffix}")
                    if volume.id != getattr(manifest, name_field):
                        raise DomainError(
                            "volume_identity", "Persistent volume identity changed", 409
                        )
                self.save(manifest, "starting")
            return self.start(name)

    def discover(self, sdk, manifest):
        candidates = list(
            sdk.list(
                ListSandboxesQuery(
                    labels={
                        "app": "voice-studio",
                        "deployment": manifest.deployment,
                        "role": "single",
                    }
                )
            )
        )
        if len(candidates) > 1:
            raise DomainError(
                "duplicate_writers",
                "Multiple stateful sandboxes exist; reconcile before starting",
                409,
            )
        if candidates:
            if (
                manifest.key_fingerprint
                and candidates[0].labels.get("key_fingerprint") != manifest.key_fingerprint
            ):
                raise DomainError(
                    "recovery_keys_required",
                    "The running deployment belongs to different recovery keys",
                    409,
                )
            if manifest.sandbox_id and candidates[0].id != manifest.sandbox_id:
                raise DomainError(
                    "sandbox_mismatch",
                    "Unexpected stateful sandbox; recovery must be explicit",
                    409,
                )
            return candidates[0]
        if manifest.sandbox_id:
            try:
                sdk.get(manifest.sandbox_id)
            except Exception as exc:
                if not missing(exc):
                    raise
            else:
                raise DomainError(
                    "sandbox_labels", "Recorded sandbox exists but its ownership labels differ", 409
                )
        return None

    def start(self, name, _locked=False):
        from contextlib import nullcontext

        with nullcontext() if _locked else FileLock(str(self.path(name)) + ".lock", timeout=0):
            manifest = self.load(name)
            if (
                not manifest.objects_volume
                or not manifest.checkpoints_volume
                or not manifest.snapshot
            ):
                raise DomainError(
                    "setup_incomplete", "Rerun setup to finish snapshot and volume preparation", 409
                )
            if manifest.phase == "purged":
                raise DomainError(
                    "deployment_destroyed",
                    "This deployment was explicitly destroyed; choose a new deployment name",
                    410,
                )
            if manifest.update_phase and not _locked:
                raise DomainError(
                    "update_pending", "Resume the explicit update or rollback command", 409
                )
            sdk = self.connect(name)
            sandbox = self.discover(sdk, manifest)
            if sandbox is None:
                manifest.sandbox_id = None
                self.preflight(sdk, manifest)
                self.save(manifest, "creating")
                # Provider-unique stable name is the single-writer create boundary.
                sandbox = sdk.create(
                    CreateSandboxFromSnapshotParams(
                        name=f"voice-single-{name}",
                        snapshot=manifest.snapshot,
                        public=False,
                        os_user="root",
                        ephemeral=False,
                        labels={
                            "app": "voice-studio",
                            "deployment": name,
                            "role": "single",
                            "key_fingerprint": manifest.key_fingerprint,
                        },
                        auto_stop_interval=30,
                        auto_archive_interval=0,
                        auto_delete_interval=-1,
                        volumes=[
                            VolumeMount(
                                volume_id=manifest.objects_volume, mount_path="/mnt/voice-objects"
                            ),
                            VolumeMount(
                                volume_id=manifest.checkpoints_volume,
                                mount_path="/mnt/voice-checkpoints",
                            ),
                        ],
                    ),
                    timeout=180,
                )
                manifest.sandbox_id = sandbox.id
                self.save(manifest, "bootstrapping")
            elif state_name(sandbox) in {"stopped", "archived"}:
                sandbox.start(timeout=180)
            elif state_name(sandbox) != "started":
                raise DomainError(
                    "session_transition", "The sandbox is still changing state; retry status", 409
                )
            if manifest.sandbox_id != sandbox.id:
                manifest.sandbox_id = sandbox.id
                self.save(manifest, "reconnecting")
            # Never delete/restart a running supervisor. flock also guards remote races.
            status = sandbox.process.exec("/opt/voice/.venv/bin/voice single control status")
            if status.exit_code != 0:
                credential = self.vault.get(name)
                sandbox.fs.create_folder("/dev/shm/voice-bootstrap", "0700")
                payload = {
                    **credential,
                    "deployment": manifest.model_dump(),
                    "sandbox_id": sandbox.id,
                }
                sandbox.fs.upload_file(
                    json.dumps(payload).encode(), "/dev/shm/voice-bootstrap/config.json"
                )
                session_name = "voice-single-" + secrets.token_hex(8)
                sandbox.process.create_session(session_name)
                result = sandbox.process.execute_session_command(
                    session_name,
                    SessionExecuteRequest(
                        command="chmod 600 /dev/shm/voice-bootstrap/config.json && /opt/voice/.venv/bin/voice single supervise",
                        run_async=True,
                        suppress_input_echo=True,
                    ),
                )
                if not result.cmd_id:
                    raise DomainError("bootstrap_failed", "Supervisor did not start", 503)
            self.save(manifest, "checking")
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                result = sandbox.process.exec("/opt/voice/.venv/bin/voice single control status")
                if result.exit_code == 0:
                    ready = json.loads(result.result)
                    if ready.get("ready"):
                        manifest.initialized = True
                        self.save(manifest, "ready")
                        signed = sandbox.create_signed_preview_url(8765, expires_in_seconds=3600)
                        return {
                            "deployment": name,
                            "sandbox_id": sandbox.id,
                            "state": "ready",
                            "url": signed.url,
                            "expires_at": time.time() + 3600,
                            "model_ready": ready.get("model_ready", False),
                            "database_ready": ready.get("database_ready", False),
                            "profile": PROFILE,
                        }
                time.sleep(2)
            raise DomainError(
                "startup_timeout",
                "Studio did not become ready; inspect cloud status and retry without replacing storage",
                503,
            )

    def control(self, name, action):
        if action not in {"status", "backup", "stop"}:
            raise ValueError("Unsupported control action")
        manifest = self.load(name)
        sdk = self.connect(name)
        sandbox = self.discover(sdk, manifest)
        metadata = {
            "deployment": name,
            "profile": manifest.profile,
            "phase": manifest.phase,
            "snapshot": manifest.snapshot,
            "source_sha256": manifest.source_sha256,
            "database_schema": manifest.database_schema,
            "resources": {
                "cpu": manifest.cpu,
                "memory_gib": manifest.memory,
                "disk_gib": manifest.disk,
            },
            "daytona_qualified": False,
        }
        if sandbox is None or state_name(sandbox) != "started":
            return {
                **metadata,
                "state": state_name(sandbox) if sandbox else "missing",
                "ready": False,
            }
        result = sandbox.process.exec("/opt/voice/.venv/bin/voice single control " + action)
        if result.exit_code:
            raise DomainError(
                "control_unavailable",
                "Supervisor is unavailable; no destructive fallback was attempted",
                503,
            )
        response = json.loads(result.result)
        if action == "stop" and response.get("stopped"):
            sandbox.stop(timeout=120)
        return {**metadata, **response}

    def build_candidate(self, sdk, manifest, source):
        with tempfile.TemporaryDirectory() as context:
            digest, inventory = source_context(source, context)
            name = f"voice-single-{digest[:24]}"
            try:
                snapshot = sdk.snapshot.get(name)
            except Exception as exc:
                if not missing(exc):
                    raise
                snapshot = sdk.snapshot.create(
                    CreateSnapshotParams(
                        name=name,
                        image=Image.from_dockerfile(
                            Path(context) / "Dockerfile", strict_context=True
                        ),
                        resources=Resources(cpu=4, memory=8, disk=20),
                        region_id=manifest.region,
                    ),
                    timeout=1800,
                )
            if state_name(snapshot) != "active":
                raise DomainError(
                    "candidate_build_pending",
                    "Candidate snapshot is not complete; rerun update",
                    503,
                )
            self.validate_snapshot(snapshot)
            return name, digest, inventory

    @staticmethod
    def validate_snapshot(snapshot):
        if (snapshot.cpu, snapshot.mem, snapshot.disk, snapshot.gpu) != (4, 8, 20, 0):
            raise DomainError(
                "snapshot_resources",
                "Snapshot resources differ from the approved CPU allocation",
                409,
            )

    def update(self, name, source=None, rollback=False):
        """Journal every destructive boundary. The old writer stops before replacement."""
        with FileLock(str(self.path(name)) + ".lock", timeout=0):
            manifest = self.load(name)
            sdk = self.connect(name)
            if rollback:
                if not manifest.previous_snapshot:
                    raise DomainError(
                        "rollback_unavailable", "No compatible previous image is recorded", 409
                    )
                candidate, digest, inventory = (
                    manifest.previous_snapshot,
                    manifest.previous_source_sha256,
                    manifest.previous_source_inventory,
                )
            elif source:
                candidate, digest, inventory = self.build_candidate(sdk, manifest, source)
            else:
                raise DomainError(
                    "source_required", "Update requires an explicit approved source directory", 400
                )
            if rollback and manifest.update_phase == "restoring":
                # Explicit rollback of an unpromoted replacement uses the newest checkpoint.
                # Stop is provider-confirmed before deletion; no older database is selected.
                failed = self.discover(sdk, manifest)
                if failed is not None:
                    if state_name(failed) == "started":
                        failed.stop(timeout=180)
                    failed = sdk.get(failed.id)
                    if state_name(failed) != "stopped":
                        raise DomainError(
                            "writer_active",
                            "Rollback requires a confirmed stopped replacement",
                            409,
                        )
                    failed.delete(timeout=180, wait=True)
                manifest.snapshot, manifest.source_sha256 = candidate, digest
                manifest.source_inventory = inventory
                manifest.snapshot_id = sdk.snapshot.get(candidate).id
                manifest.sandbox_id = None
                manifest.candidate_snapshot = candidate
                self.save(manifest, "restoring")
            if manifest.candidate_snapshot and manifest.candidate_snapshot != candidate:
                raise DomainError(
                    "update_pending",
                    "Resume the recorded candidate before selecting another image",
                    409,
                )
            if candidate == manifest.snapshot and not manifest.update_phase:
                return self.start(name, _locked=True)
            manifest.candidate_snapshot = candidate
            if not manifest.update_phase:
                manifest.update_phase = "validating"
                self.save(manifest, "validating")
            if manifest.update_phase == "validating":
                quota_request = manifest.model_copy(update={"sandbox_id": None})
                self.preflight(sdk, quota_request)
                # Candidate has no production volumes, database or credentials.
                candidate_name = f"voice-check-{name}-{candidate[-12:]}"
                candidate_labels = {"app": "voice-studio", "deployment": name, "role": "candidate"}
                try:
                    previous_check = sdk.get(candidate_name)
                except Exception as exc:
                    if not missing(exc):
                        raise
                else:
                    if any(previous_check.labels.get(k) != v for k, v in candidate_labels.items()):
                        raise DomainError(
                            "sandbox_labels", "Candidate ownership could not be verified", 409
                        )
                    # Isolated validation data is disposable. A fresh candidate also
                    # reconciles a prior validation command whose response was lost.
                    previous_check.delete(timeout=120, wait=True)
                check = sdk.create(
                    CreateSandboxFromSnapshotParams(
                        name=candidate_name,
                        snapshot=candidate,
                        public=False,
                        os_user="root",
                        auto_stop_interval=10,
                        auto_delete_interval=10,
                        labels=candidate_labels,
                    ),
                    timeout=180,
                )
                result = check.process.exec(
                    "/opt/voice/.venv/bin/voice single validate", timeout=180
                )
                if (
                    result.exit_code
                    or json.loads(result.result).get("schema") != manifest.database_schema
                ):
                    # Validation has not touched the current writer or its volumes.
                    # Keep normal starts available and allow a corrected candidate.
                    manifest.candidate_snapshot = manifest.update_phase = None
                    self.save(manifest, "candidate_rejected")
                    raise DomainError(
                        "schema_incompatible",
                        "Candidate failed validation or cannot preserve the current schema",
                        409,
                    )
                check.delete(timeout=120, wait=True)
                manifest.update_phase = "draining"
                self.save(manifest, "draining")
            if manifest.update_phase == "draining":
                old = self.discover(sdk, manifest)
                if old is None:
                    raise DomainError(
                        "writer_missing", "Recover the current deployment before updating", 409
                    )
                if state_name(old) != "started":
                    self.start(name, _locked=True)
                response = self.control(name, "stop")
                if not response.get("stopped"):
                    raise DomainError(
                        "deployment_busy", "Finish or cancel outstanding jobs before updating", 409
                    )
                manifest.update_phase = "replacing"
                self.save(manifest, "replacing")
            if manifest.update_phase == "replacing":
                old = self.discover(sdk, manifest)
                if old is not None:
                    if state_name(old) != "stopped":
                        raise DomainError(
                            "writer_active", "Replacement requires a confirmed stopped writer", 409
                        )
                    old.delete(timeout=180, wait=True)
                # This checkpoint-only recovery boundary is explicit and journaled.
                manifest.previous_source_inventory = manifest.source_inventory
                manifest.previous_snapshot, manifest.previous_source_sha256 = (
                    manifest.snapshot,
                    manifest.source_sha256,
                )
                manifest.snapshot, manifest.source_sha256 = candidate, digest
                manifest.source_inventory = inventory
                manifest.snapshot_id = sdk.snapshot.get(candidate).id
                manifest.sandbox_id = None
                manifest.update_phase = "restoring"
                self.save(manifest, "restoring")
            try:
                result = self.start(name, _locked=True)
            except Exception:
                # Leave the journal intact; no ambiguous error triggers another writable runtime.
                self.save(self.load(name), "update_needs_recovery")
                raise
            manifest = self.load(name)
            manifest.candidate_snapshot = manifest.update_phase = None
            self.save(manifest, "ready")
            return {**result, "updated": True, "rollback": rollback}

    def destroy(self, name, purge_data=False, confirm=""):
        if purge_data and confirm != name:
            raise DomainError(
                "confirmation_required",
                "Purging storage requires --confirm matching the deployment name",
                400,
            )
        with FileLock(str(self.path(name)) + ".lock", timeout=0):
            manifest = self.load(name)
            sdk = self.connect(name)
            sandbox = self.discover(sdk, manifest)
            if sandbox is not None:
                if state_name(sandbox) == "started":
                    if not self.control(name, "stop").get("stopped"):
                        raise DomainError(
                            "deployment_busy",
                            "Finish or cancel active work before destroying compute",
                            409,
                        )
                    sandbox = sdk.get(sandbox.id)
                if state_name(sandbox) not in {"stopped", "archived"}:
                    raise DomainError(
                        "session_transition", "Wait for a confirmed stopped sandbox", 409
                    )
                sandbox.delete(timeout=180, wait=True)
            manifest.sandbox_id = None
            self.save(manifest, "compute_destroyed")
            if purge_data:
                for identifier, suffix in (
                    (manifest.objects_volume, "objects"),
                    (manifest.checkpoints_volume, "checkpoints"),
                ):
                    try:
                        volume = sdk.volume.get(f"voice-{name}-{suffix}")
                    except Exception as exc:
                        if not missing(exc):
                            raise
                        continue
                    if volume.id != identifier:
                        raise DomainError(
                            "volume_identity", "Storage identity changed; refusing to delete", 409
                        )
                    sdk.volume.delete(volume)
                self.save(manifest, "purged")
            return {"deployment": name, "destroyed": True, "volumes_retained": not purge_data}

    def recovery_export(self, name, passphrase, output):
        if len(passphrase) < 16:
            raise ValueError("Use a recovery passphrase of at least sixteen characters")
        salt, nonce = os.urandom(16), os.urandom(12)
        key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())
        data = json.dumps(
            {"manifest": self.load(name).model_dump(), "credentials": self.vault.get(name)}
        ).encode()
        path = Path(output)
        with path.open("xb") as stream:
            os.chmod(path, 0o600)
            stream.write(
                b"VSR1" + salt + nonce + AESGCM(key).encrypt(nonce, data, b"voice-recovery-v1")
            )
        return {"exported": str(path), "encrypted": True}

    def recovery_import(self, path, passphrase):
        data = Path(path).read_bytes()
        if data[:4] != b"VSR1" or len(data) > 1_000_000:
            raise ValueError("Invalid recovery bundle")
        key = Scrypt(salt=data[4:20], length=32, n=2**15, r=8, p=1).derive(passphrase.encode())
        value = json.loads(AESGCM(key).decrypt(data[20:32], data[32:], b"voice-recovery-v1"))
        manifest = DeploymentManifest.model_validate(value["manifest"])
        if self.exists(manifest.deployment):
            existing = self.load(manifest.deployment)
            fingerprint = hashlib.sha256(value["credentials"]["backup_key"].encode()).hexdigest()
            if (
                self.vault.get(manifest.deployment) is not None
                or existing.key_fingerprint != fingerprint
                or any(
                    getattr(existing, field) != getattr(manifest, field)
                    for field in ("account", "objects_volume", "checkpoints_volume")
                )
            ):
                raise ValueError(
                    "Deployment already configured; refusing to overwrite recovery keys"
                )
            self.vault.put(manifest.deployment, value["credentials"])
            return {"deployment": manifest.deployment, "credentials_restored": True}
        self.vault.put(manifest.deployment, value["credentials"])
        self.save(manifest, "recovered")
        return {"deployment": manifest.deployment, "imported": True}
