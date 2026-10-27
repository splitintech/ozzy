"""Fault-injection tests for single-sandbox publication and owner launch boundaries."""

import base64
import io
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from voice_studio_core.checkpoints import CheckpointStore, DurabilityCoordinator
from voice_studio_core.config import Settings
from voice_studio_core.contracts import DomainError
from voice_studio_core.volume_storage import VolumeStorage
from voice_studio_service.api import create_app
from voice_studio_service.deployment import (
    DeploymentManager,
    DeploymentManifest,
    allowed_source,
    source_context,
    unpack_context,
)
from voice_studio_service.durability import request_fingerprint

KEY = base64.b64encode(b"x" * 32).decode()
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    return CheckpointStore(tmp_path / "checkpoints", KEY, "test")


def test_profiles_remain_explicit():
    assert Settings(_env_file=None).profile == "local"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, profile="hosted_daytona_single")
    cfg = Settings(
        _env_file=None,
        profile="hosted_daytona_single",
        backup_key=KEY,
        database_url="postgresql+psycopg://test/db",
        owner_password_hash="hash",
        session_secret="s" * 32,
    )
    assert cfg.storage_backend == "daytona_volume" and not cfg.s3_bucket
    with pytest.raises(ValidationError):
        DeploymentManifest(deployment="test", cpu=8)


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "data/studio.sqlite3",
        "models/voice.safetensors",
        "python/core/src/voice_studio_core/passwords.json",
        "apps/studio/src/unrelated.ts",
        "../secret",
        "/tmp/secret",
    ],
)
def test_build_inventory_rejects_unapproved_files(name):
    assert not allowed_source(name)


def test_build_hash_and_packaged_sources(tmp_path):
    first = tmp_path / "one"
    digest, inventory = source_context(ROOT, first)
    assert len(digest) == 64 and "deploy/Dockerfile" in inventory
    (first / ".env").write_text("DO_NOT_UPLOAD=secret")
    second, _ = source_context(first, tmp_path / "two")
    assert second == digest
    assert not (tmp_path / "two/.env").exists()
    target = first / "apps/studio/src/App.tsx"
    target.write_text(target.read_text() + "\n// modified source\n")
    changed, _ = source_context(first, tmp_path / "three")
    assert changed != digest


def test_source_symlink_rejected(tmp_path):
    source_context(ROOT, tmp_path / "one")
    path = tmp_path / "one/pyproject.toml"
    path.unlink()
    path.symlink_to(ROOT / "pyproject.toml")
    with pytest.raises(ValueError, match="symlinks"):
        source_context(tmp_path / "one", tmp_path / "two")


def test_bundle_traversal_rejected(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        item = tarfile.TarInfo("../escape")
        item.size = 4
        archive.addfile(item, io.BytesIO(b"nope"))
    buffer.seek(0)
    with pytest.raises(ValueError):
        unpack_context(buffer, tmp_path)


def test_volume_only_verified_immutable_objects_are_readable(tmp_path):
    storage = VolumeStorage(tmp_path / "volume")
    file = tmp_path / "audio"
    file.write_bytes(b"finished audio")
    path = storage.path("voices/one/reference.wav")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"partial")
    with pytest.raises(FileNotFoundError):
        storage.read("voices/one/reference.wav")
    receipt = storage.put("voices/one/reference.wav", file)
    assert storage.read("voices/one/reference.wav") == b"finished audio"
    assert storage.put("voices/one/reference.wav", file) == receipt
    file.write_bytes(b"other audio")
    with pytest.raises(DomainError, match="overwritten"):
        storage.put("voices/one/reference.wav", file)
    path.write_bytes(b"corrupted")
    with pytest.raises(DomainError):
        storage.read("voices/one/reference.wav")
    storage.delete_prefix("voices/one")
    assert not path.exists()


@pytest.mark.parametrize("failure", ["partial", "delayed", "unavailable"])
def test_volume_write_failure_cannot_publish(tmp_path, monkeypatch, failure):
    storage = VolumeStorage(tmp_path / "volume")
    file = tmp_path / "source"
    file.write_bytes(b"complete")
    import shutil

    if failure == "partial":
        monkeypatch.setattr(shutil, "copyfile", lambda _, dst: Path(dst).write_bytes(b"part"))
    elif failure == "delayed":
        monkeypatch.setattr(shutil, "copyfile", lambda *_: None)
    else:
        monkeypatch.setattr(shutil, "copyfile", Mock(side_effect=OSError("unavailable")))
    with pytest.raises((DomainError, OSError)):
        storage.put("expiring/jobs/one/audio.wav", file)
    assert not storage._marker("expiring/jobs/one/audio.wav").exists()


def test_encrypted_checkpoints_keep_two_verified_generations(store):
    for revision in range(1, 5):
        store.write(revision, b"private SQL", [], {"voices": ["deleted"], "jobs": []})
    assert len(store.commits()) == 2
    latest, dump = store.load()
    assert latest["revision"] == 4 and dump == b"private SQL"
    assert latest["tombstones"]["voices"] == ["deleted"]
    assert b"private SQL" not in next(store.root.glob("*.dump.aes")).read_bytes()


@pytest.mark.parametrize("part", ["manifest", "data", "wrong_key"])
def test_corrupt_newest_checkpoint_never_falls_back(store, part):
    store.write(1, b"old", [], {})
    store.write(2, b"acknowledged new", [], {})
    if part == "manifest":
        store.commits()[-1].write_text("{}")
    elif part == "data":
        (store.root / "00000000000000000002.dump.aes").write_bytes(b"broken")
    else:
        store.key = b"y" * 32
    with pytest.raises(DomainError, match="fallback"):
        store.load()


def test_incomplete_upload_is_not_a_restore_candidate(store):
    store.write(1, b"complete", [], {})
    (store.root / "00000000000000000002.dump.aes").write_bytes(b"partial")
    assert store.load()[1] == b"complete"


def test_checkpoint_failure_blocks_ack_and_retry_preserves_receipt(repo, tmp_path, monkeypatch):
    repo.settings.backup_key = KEY  # constructed coordinator below uses SecretStr
    from pydantic import SecretStr

    repo.settings.backup_key = SecretStr(KEY)
    repo.settings.checkpoint_dir = tmp_path / "checkpoints"
    coordinator = DurabilityCoordinator(
        repo, VolumeStorage(tmp_path / "objects"), dump=lambda: b"database"
    )
    coordinator.record("operation", "fingerprint", {"status": 201, "key": "private-key"})
    original = coordinator.store.write
    monkeypatch.setattr(coordinator.store, "write", Mock(side_effect=OSError("volume unavailable")))
    with pytest.raises(OSError):
        coordinator.checkpoint()
    assert coordinator.blocked
    monkeypatch.setattr(coordinator.store, "write", original)
    coordinator.ensure_available()
    assert not coordinator.blocked
    assert coordinator.receipt("operation", "fingerprint")["key"] == "private-key"
    with repo.engine.connect() as c:
        assert (
            "private-key"
            not in c.execute(text("SELECT response FROM operation_receipts")).scalar_one()
        )
    with pytest.raises(DomainError):
        coordinator.receipt("operation", "different request")


def test_semantic_multipart_idempotency():
    def body(boundary):
        return f'--{boundary}\r\nContent-Disposition: form-data; name="name"\r\n\r\nMy voice\r\n--{boundary}--\r\n'.encode()

    assert request_fingerprint(
        "POST", "/v1/voices", body("a"), "multipart/form-data; boundary=a"
    ) == request_fingerprint("POST", "/v1/voices", body("b"), "multipart/form-data; boundary=b")


class MemoryVault:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def put(self, name, value):
        self.values[name] = value


def manager(tmp_path):
    vault = MemoryVault()
    vault.put(
        "test",
        {
            "api_key": "provider-secret",
            "backup_key": KEY,
            "session_secret": "s" * 32,
            "owner_password_hash": "hash",
        },
    )
    result = DeploymentManager(tmp_path, vault=vault, sdk=Mock())
    result.save(
        DeploymentManifest(
            deployment="test",
            sandbox_id="sandbox",
            snapshot="snapshot",
            objects_volume="objects",
            checkpoints_volume="checkpoints",
            initialized=True,
        )
    )
    return result


def test_uncertain_provider_response_never_creates_replacement(tmp_path):
    deployment = manager(tmp_path)
    deployment.sdk.list.side_effect = TimeoutError("uncertain")
    with pytest.raises(TimeoutError):
        deployment.start("test")
    deployment.sdk.create.assert_not_called()
    deployment.sdk.delete.assert_not_called()


def test_duplicate_writers_fail_closed(tmp_path):
    deployment = manager(tmp_path)
    deployment.sdk.list.return_value = [NS(id="one"), NS(id="two")]
    with pytest.raises(DomainError, match="Multiple"):
        deployment.start("test")
    deployment.sdk.create.assert_not_called()


def test_stop_never_forces_busy_database(tmp_path):
    deployment = manager(tmp_path)
    sandbox = NS(id="sandbox", state="started", process=Mock(), stop=Mock())
    sandbox.process.exec.return_value = NS(
        exit_code=0, result=json.dumps({"drained": False, "active_jobs": 1})
    )
    deployment.sdk.list.return_value = [sandbox]
    assert deployment.control("test", "stop")["active_jobs"] == 1
    sandbox.stop.assert_not_called()


def test_recovery_bundle_requires_correct_separate_passphrase(tmp_path):
    deployment = manager(tmp_path / "first")
    bundle = tmp_path / "recovery.vsr"
    deployment.recovery_export("test", "separate passphrase long enough", bundle)
    assert b"provider-secret" not in bundle.read_bytes()
    assert bundle.stat().st_mode & 0o077 == 0
    recovered = DeploymentManager(tmp_path / "second", vault=MemoryVault())
    with pytest.raises(Exception):
        recovered.recovery_import(bundle, "wrong")
    recovered.recovery_import(bundle, "separate passphrase long enough")
    assert recovered.vault.get("test")["backup_key"] == KEY
    assert "provider-secret" not in recovered.path("test").read_text()
    with pytest.raises(ValueError):
        recovered.recovery_import(bundle, "separate passphrase long enough")


def test_local_launcher_authentication_and_host_boundary(config, repo, objects):
    app = create_app(config, repo, objects)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 1234)) as client:
        assert client.get("/launcher/deployments").status_code == 403
        headers = {"Origin": "http://127.0.0.1:8765", "Authorization": "Bearer test-local-token"}
        assert client.get("/launcher/deployments", headers=headers).status_code == 200
        assert (
            client.get(
                "/launcher/deployments", headers={**headers, "Host": "evil.example"}
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/launcher/deployments", headers={**headers, "Origin": "http://evil.example"}
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/launcher/deployments", headers={**headers, "Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )


def test_pinned_sdk_remote_build_and_lifecycle_contract():
    from daytona import CreateSandboxFromSnapshotParams, Image
    from daytona_api_client.models.sandbox_class import SandboxClass

    Image.from_dockerfile(ROOT / "deploy/Dockerfile")
    assert "container" in [item.value for item in SandboxClass]
    params = CreateSandboxFromSnapshotParams(
        snapshot="test",
        name="voice-single-test",
        auto_delete_interval=-1,
        auto_archive_interval=0,
        public=False,
    )
    assert params.auto_delete_interval == -1


@pytest.mark.parametrize("boundary", ["dump", "upload", "marker", "after_marker", "lost_response"])
def test_mutation_ack_and_retry_at_every_checkpoint_boundary(
    config, repo, tmp_path, monkeypatch, boundary
):
    from pydantic import SecretStr

    storage = VolumeStorage(tmp_path / "volume")
    config.backup_key, config.checkpoint_dir = SecretStr(KEY), tmp_path / "checkpoints"
    app = create_app(config, repo, storage)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        coordinator = DurabilityCoordinator(repo, storage, dump=lambda: b"consistent database dump")
        repo.durability = coordinator
        coordinator.checkpoint()
        original_dump, original_write = coordinator.dump, coordinator.store.write
        original_path_write = Path.write_bytes
        if boundary == "dump":
            coordinator.dump = Mock(side_effect=OSError("dump interrupted"))
        elif boundary in {"upload", "marker"}:

            def fail(path, data):
                if path.name.endswith(".dump.aes" if boundary == "upload" else ".commit.json"):
                    raise OSError("transfer interrupted")
                return original_path_write(path, data)

            monkeypatch.setattr(Path, "write_bytes", fail)
        elif boundary == "after_marker":

            def committed_then_interrupted(*args):
                original_write(*args)
                raise OSError("response interrupted after completion marker")

            coordinator.store.write = committed_then_interrupted
        headers = {
            "Authorization": "Bearer test-local-token",
            "Idempotency-Key": "retry-this-operation",
        }
        first = client.post("/v1/keys", json={"name": "Recoverable key"}, headers=headers)
        if boundary == "lost_response":
            assert first.status_code == 200
        else:
            assert first.status_code == 503, first.text
        coordinator.dump, coordinator.store.write = original_dump, original_write
        monkeypatch.setattr(Path, "write_bytes", original_path_write)
        retried = client.post("/v1/keys", json={"name": "Recoverable key"}, headers=headers)
        assert retried.status_code == 200, retried.text
        repeated = client.post("/v1/keys", json={"name": "Recoverable key"}, headers=headers)
        assert repeated.json() == retried.json()
        assert len(client.get("/v1/keys", headers=headers).json()) == 1
        assert coordinator.store.load()[0]["revision"] >= 2
        if boundary == "lost_response":
            assert first.json() == retried.json()


def test_repeated_setup_reuses_resources_and_password(tmp_path, monkeypatch):
    sdk = Mock()
    sdk.snapshot.get.return_value = NS(
        id="snapshot-id", state="active", cpu=4, mem=8, disk=20, gpu=0
    )
    from daytona import DaytonaNotFoundError

    sdk.list.return_value = []

    created_volumes = set()

    def volume_get(name, create=False):
        if not create and name not in created_volumes:
            raise DaytonaNotFoundError("not found", status_code=404)
        created_volumes.add(name)
        return NS(id=name, state="ready")

    sdk.volume.get.side_effect = volume_get
    vault = MemoryVault()
    manager = DeploymentManager(tmp_path, sdk=sdk, vault=vault)
    monkeypatch.setattr(manager, "preflight", lambda *_: None)
    monkeypatch.setattr(manager, "start", lambda name: {"deployment": name, "ready": True})
    manager.setup(
        "test",
        source=ROOT,
        api_key="protected provider key",
        owner_password="original password long enough",
    )
    original = dict(vault.get("test"))
    manager.setup(
        "test", source=ROOT, api_key="ignored replacement", owner_password="replacement password"
    )
    assert vault.get("test") == original
    assert sdk.volume.get.call_count == 6
    sdk.snapshot.create.assert_not_called()
    assert "protected provider key" not in manager.path("test").read_text()


def test_stopped_sandbox_restarts_without_rebuilding_or_deleting(tmp_path):
    deployment = manager(tmp_path)
    sandbox = Mock(id="sandbox", state="stopped")
    sandbox.process.exec.return_value = NS(exit_code=0, result='{"ready":true}')
    sandbox.create_signed_preview_url.return_value = NS(url="https://private.example/studio")
    deployment.sdk.list.return_value = [sandbox]
    result = deployment.start("test")
    assert result["state"] == "ready"
    sandbox.start.assert_called_once()
    sandbox.delete.assert_not_called()
    deployment.sdk.create.assert_not_called()
    deployment.sdk.snapshot.create.assert_not_called()
    sandbox.fs.upload_file.assert_not_called()


def test_failed_candidate_does_not_stop_current_writer(tmp_path, monkeypatch):
    deployment = manager(tmp_path)
    current = deployment.load("test")
    current.snapshot = "old-image"
    deployment.save(current)
    monkeypatch.setattr(deployment, "preflight", lambda *_: None)
    monkeypatch.setattr(deployment, "build_candidate", lambda *_: ("new-image", "digest", {}))
    check = Mock()
    check.process.exec.return_value = NS(exit_code=1, result="")
    deployment.sdk.get.return_value = check
    check.labels = {"app": "voice-studio", "deployment": "test", "role": "candidate"}
    deployment.sdk.create.return_value = check
    with pytest.raises(DomainError, match="Candidate"):
        deployment.update("test", source=ROOT)
    deployment.sdk.list.assert_not_called()
    check.stop.assert_not_called()
    assert deployment.load("test").snapshot == "old-image"
    assert deployment.load("test").update_phase is None


def test_hosted_page_has_no_launcher_routes(config, repo, objects):
    config.mode = "hosted"
    config.profile = "hosted_external"
    app = create_app(config, repo, objects)
    assert not any(route.path.startswith("/launcher/") for route in app.routes)


@pytest.mark.parametrize(
    "active_jobs,idle,expected_stop", [(0, 601, True), (1, 1200, False), (0, 100, False)]
)
def test_idle_shutdown_waits_for_jobs_and_refreshes_backstop(
    monkeypatch, active_jobs, idle, expected_stop
):
    import time

    import daytona
    from voice_studio_service.single import Supervisor

    payload = {
        "api_key": "private fixture",
        "sandbox_id": "test",
        "deployment": {"deployment": "test", "region": None, "idle_seconds": 600},
    }
    supervisor = Supervisor(payload)
    supervisor.booted = time.time() - idle
    supervisor.done = Mock()
    supervisor.done.wait.side_effect = [False, True]
    supervisor.dispatch = Mock(
        side_effect=[
            {"last_activity": time.time() - idle, "active_jobs": active_jobs},
            {"stopped": True},
        ]
    )
    sandbox = Mock()
    sdk = Mock()
    sdk.get.return_value = sandbox
    monkeypatch.setattr(daytona, "Daytona", lambda *_: sdk)
    supervisor.monitor()
    assert sandbox.stop.called == expected_stop
    assert sandbox.refresh_activity.called == (not expected_stop)


def test_checkpoint_failure_prevents_idle_provider_stop(monkeypatch):
    import time

    import daytona
    from voice_studio_service.single import Supervisor

    supervisor = Supervisor(
        {
            "api_key": "fixture",
            "sandbox_id": "test",
            "deployment": {"deployment": "test", "region": None, "idle_seconds": 600},
        }
    )
    supervisor.booted = time.time() - 1200
    supervisor.done = Mock()
    supervisor.done.wait.side_effect = [False, True]
    supervisor.dispatch = Mock(
        side_effect=[
            {"last_activity": time.time() - 1200, "active_jobs": 0},
            OSError("checkpoint failed"),
        ]
    )
    sdk = Mock()
    monkeypatch.setattr(daytona, "Daytona", lambda *_: sdk)
    supervisor.monitor()
    sdk.get.return_value.stop.assert_not_called()
    assert supervisor.state == "storage_unavailable"


def test_update_stops_old_writer_before_replacement_and_keeps_latest_data(tmp_path, monkeypatch):
    deployment = manager(tmp_path)
    monkeypatch.setattr(deployment, "preflight", lambda *_: None)
    monkeypatch.setattr(deployment, "build_candidate", lambda *_: ("candidate", "new-digest", {}))
    order = []
    old = Mock(id="sandbox", state="started")
    candidate = Mock()
    candidate.process.exec.return_value = NS(exit_code=0, result='{"schema":2,"validated":true}')
    candidate.delete.side_effect = lambda **_: order.append("candidate-validated")
    candidate.labels = {"app": "voice-studio", "deployment": "test", "role": "candidate"}
    deployment.sdk.get.return_value = candidate
    deployment.sdk.create.return_value = candidate
    deployment.sdk.snapshot.get.return_value = NS(id="candidate-id")
    deployment.sdk.list.side_effect = lambda *_: [] if "old-deleted" in order else [old]

    def stop(name, action):
        assert action == "stop"
        order.append("checkpoint-and-stop")
        old.state = "stopped"
        return {"stopped": True}

    monkeypatch.setattr(deployment, "control", stop)
    old.delete.side_effect = lambda **_: order.append("old-deleted")

    def start(name, _locked=False):
        assert "old-deleted" in order
        state = deployment.load(name)
        assert state.update_phase == "restoring" and state.initialized
        order.append("restore-latest")
        state.sandbox_id = "replacement"
        deployment.save(state)
        return {"ready": True}

    monkeypatch.setattr(deployment, "start", start)
    assert deployment.update("test", source=ROOT)["updated"]
    assert order == [
        "candidate-validated",
        "candidate-validated",
        "checkpoint-and-stop",
        "old-deleted",
        "restore-latest",
    ]
    state = deployment.load("test")
    assert state.previous_snapshot == "snapshot"
    assert state.snapshot == "candidate" and state.update_phase is None


def test_operation_replay_cannot_return_deleted_voice(config, repo, tmp_path, recording):
    from pydantic import SecretStr

    storage = VolumeStorage(tmp_path / "volume")
    config.backup_key, config.checkpoint_dir = SecretStr(KEY), tmp_path / "checkpoints"
    with TestClient(create_app(config, repo, storage), base_url="http://127.0.0.1:8765") as client:
        repo.durability = DurabilityCoordinator(repo, storage, dump=lambda: b"dump")
        client.headers["Authorization"] = "Bearer test-local-token"
        arguments = dict(
            headers={"Idempotency-Key": "voice-import"},
            files={"file": ("fixture.wav", recording.read_bytes(), "audio/wav")},
            data={"name": "Test", "authorized": "true", "start": 0, "end": 8},
        )
        voice = client.post("/v1/voices", **arguments)
        assert voice.status_code == 201, voice.text
        assert client.delete("/v1/voices/" + voice.json()["id"]).status_code == 200
        replay = client.post("/v1/voices", **arguments)
        assert replay.status_code == 410, replay.text
        assert not client.get("/v1/voices").json()


def test_orphan_cleanup_does_not_delete_active_or_referenced_objects(tmp_path):
    import time

    storage = VolumeStorage(tmp_path / "volume")
    source = tmp_path / "source"
    source.write_bytes(b"data")
    for key in ("voices/kept/reference", "expiring/old/partial", "expiring/active/partial"):
        storage.put(key, source)
    old = time.time() - 90000
    os.utime(storage.path("voices/kept/reference"), (old, old))
    os.utime(storage.path("expiring/old/partial"), (old, old))
    assert storage.cleanup_orphans(["voices/kept/reference"]) == 1
    assert storage.read("voices/kept/reference") == b"data"
    assert storage.read("expiring/active/partial") == b"data"


def test_existing_storage_requires_original_recovery_keys(tmp_path, monkeypatch):
    sdk = Mock()
    sdk.list.return_value = []
    sdk.volume.get.return_value = NS(id="existing-storage")
    deployment = DeploymentManager(tmp_path, vault=MemoryVault(), sdk=sdk)
    monkeypatch.setattr(deployment, "preflight", lambda *_: None)
    with pytest.raises(DomainError, match="recovery bundle"):
        deployment.setup(
            "test", source=ROOT, api_key="new key", owner_password="new owner password"
        )
    assert deployment.vault.get("test") is None
    sdk.create.assert_not_called()
    sdk.snapshot.create.assert_not_called()


def test_different_launcher_keys_cannot_bootstrap_existing_writer(tmp_path):
    deployment = manager(tmp_path)
    manifest = deployment.load("test")
    manifest.key_fingerprint = "original-key-fingerprint"
    deployment.save(manifest)
    sandbox = Mock(id="sandbox", state="started")
    sandbox.labels = {"key_fingerprint": "another-owner-key"}
    deployment.sdk.list.return_value = [sandbox]
    with pytest.raises(DomainError, match="different recovery keys"):
        deployment.start("test")
    sandbox.fs.upload_file.assert_not_called()
    sandbox.start.assert_not_called()


def test_unknown_mutation_cannot_be_checkpointed_as_a_success(repo, tmp_path):
    from pydantic import SecretStr

    repo.settings.backup_key = SecretStr(KEY)
    repo.settings.checkpoint_dir = tmp_path / "checkpoints"
    coordinator = DurabilityCoordinator(
        repo, VolumeStorage(tmp_path / "volume"), dump=lambda: b"known snapshot"
    )
    coordinator.checkpoint()
    coordinator.recover_required = True
    with pytest.raises(DomainError, match="Restart"):
        coordinator.checkpoint()
    assert coordinator.store.load()[0]["revision"] == 1


def test_lost_create_response_reconciles_existing_writer(tmp_path, monkeypatch):
    from daytona import DaytonaNotFoundError

    deployment = manager(tmp_path)
    monkeypatch.setattr(deployment, "preflight", lambda *_: None)
    missing = DaytonaNotFoundError("not found", status_code=404)
    deployment.sdk.list.return_value = []
    deployment.sdk.get.side_effect = missing
    deployment.sdk.create.side_effect = TimeoutError("lost create response")
    with pytest.raises(TimeoutError):
        deployment.start("test")
    assert deployment.load("test").sandbox_id is None
    assert deployment.load("test").phase == "creating"
    sandbox = Mock(id="reconciled", state="started")
    sandbox.process.exec.return_value = NS(exit_code=0, result='{"ready":true}')
    sandbox.create_signed_preview_url.return_value = NS(url="https://private.example/studio")
    deployment.sdk.list.return_value = [sandbox]
    assert deployment.start("test")["sandbox_id"] == "reconciled"
    assert deployment.sdk.create.call_count == 1


def test_hosted_expiry_is_checkpointed_before_reference_cleanup(
    config, repo, tmp_path, recording, monkeypatch
):
    import time

    from pydantic import SecretStr
    from voice_studio_core.database import Voice

    storage = VolumeStorage(tmp_path / "volume")
    config.backup_key, config.checkpoint_dir = SecretStr(KEY), tmp_path / "checkpoints"
    with TestClient(create_app(config, repo, storage), base_url="http://127.0.0.1:8765") as client:
        coordinator = repo.durability = DurabilityCoordinator(repo, storage, dump=lambda: b"dump")
        client.headers["Authorization"] = "Bearer test-local-token"
        response = client.post(
            "/v1/voices",
            headers={"Idempotency-Key": "expiring-reference"},
            files={"file": ("fixture.wav", recording.read_bytes(), "audio/wav")},
            data={"name": "Expiring", "authorized": "true", "start": 0, "end": 8},
        )
        assert response.status_code == 201
        with repo.tx() as session:
            voice = session.get(Voice, response.json()["id"])
            voice.created_at = time.time() - 31 * 86400
            reference_key = voice.reference_key
        config.profile = "hosted_daytona_single"
        write = coordinator.store.write
        monkeypatch.setattr(
            coordinator.store, "write", Mock(side_effect=OSError("volume unavailable"))
        )
        assert client.get("/v1/voices").status_code == 503
        assert storage.read(reference_key)  # Never physically delete before durable tombstone.
        monkeypatch.setattr(coordinator.store, "write", write)
        assert client.get("/v1/voices").json() == []
        assert coordinator.store.load()[0]["objects"] == []
        assert client.get(f"/v1/voices/{voice.id}/export").status_code == 404


def test_idempotent_owner_mutation_survives_session_renewal(config, repo, tmp_path):
    from pydantic import SecretStr
    from voice_studio_core.database import Credential
    from voice_studio_service.auth import digest

    storage = VolumeStorage(tmp_path / "volume")
    config.backup_key, config.checkpoint_dir = SecretStr(KEY), tmp_path / "checkpoints"
    with TestClient(create_app(config, repo, storage), base_url="http://127.0.0.1:8765") as client:
        repo.durability = DurabilityCoordinator(repo, storage, dump=lambda: b"dump")
        config.mode = "hosted"
        with repo.tx() as session:
            for token in ("first-owner-session", "renewed-owner-session"):
                session.add(Credential(digest=digest(token), kind="session", name="Owner session"))
        responses = [
            client.post(
                "/v1/keys",
                json={"name": "Same action"},
                headers={"Authorization": "Bearer " + token, "Idempotency-Key": "lost-response"},
            )
            for token in ("first-owner-session", "renewed-owner-session")
        ]
        assert responses[0].status_code == responses[1].status_code == 200
        assert responses[0].json() == responses[1].json()
