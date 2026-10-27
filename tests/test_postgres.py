"""Real PostgreSQL integration; every test owns a fresh randomly named schema.

Set VOICE_TEST_POSTGRES_URL to an explicitly disposable PostgreSQL service. The
suite never migrates or truncates its public schema and drops only its own schema.
"""

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from voice_studio_core.config import Settings
from voice_studio_core.contracts import DomainError, GenerationRequest
from voice_studio_core.database import Artifact, Job, Repository, Runtime, Voice
from voice_studio_core.storage import LocalStorage
from voice_studio_service.jobs import submit


@pytest.fixture
def postgres_settings(tmp_path):
    dsn = os.getenv("VOICE_TEST_POSTGRES_URL")
    if not dsn:
        pytest.skip("Set VOICE_TEST_POSTGRES_URL to run isolated real PostgreSQL tests")
    url = make_url(dsn)
    if url.get_backend_name() != "postgresql":
        pytest.fail("VOICE_TEST_POSTGRES_URL must use PostgreSQL")
    # Force the installed psycopg 3 driver, retaining the caller's explicit endpoint.
    url = url.set(drivername="postgresql+psycopg")
    schema = "voice_test_" + uuid.uuid4().hex
    admin = create_engine(url, pool_pre_ping=True)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = url.update_query_dict({"options": f"-csearch_path={schema}"})
    settings = Settings(
        _env_file=None,
        mode="hosted",
        role="worker",
        s3_bucket="isolated-test-bucket",
        database_url=isolated.render_as_string(hide_password=False),
        data_dir=tmp_path / "data",
        model_dir=tmp_path / "models",
        worker_enabled=False,
    ).prepare()
    try:
        yield settings
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def postgres_repo(postgres_settings):
    repository = Repository(postgres_settings)
    repository.initialize()
    try:
        yield repository
    finally:
        repository.dispose()


def add_voice(repository):
    with repository.tx(lock=True) as session:
        voice = Voice(
            name="Postgres test",
            original_key="test",
            reference_key="test",
            checksum="0" * 64,
            details={},
            authorized=True,
            active=True,
        )
        session.add(voice)
        session.flush()
        return voice.id


def enqueue(repository, count=1):
    voice_id = add_voice(repository)
    with repository.tx(lock=True) as session:
        for index in range(count):
            session.add(
                Job(
                    voice_id=voice_id,
                    options={},
                    script_key=f"script-{index}",
                    fingerprint="0" * 64,
                    idempotency_key=f"postgres-job-{index}",
                )
            )


def parallel(count, action):
    barrier = threading.Barrier(count)

    def synchronized(index):
        barrier.wait(timeout=15)
        return action(index)

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(synchronized, range(count)))


def test_concurrent_postgres_initialize_is_serialized(postgres_settings):
    repositories = [Repository(postgres_settings) for _ in range(6)]
    try:
        parallel(len(repositories), lambda index: repositories[index].initialize())
        with repositories[0].tx() as session:
            assert session.scalar(select(func.count(Runtime.id))) == 1
            assert session.execute(text("SELECT version FROM voice_schema")).scalars().all() == [2]
            assert session.execute(text("SHOW search_path")).scalar_one().startswith("voice_test_")
    finally:
        for repository in repositories:
            repository.dispose()


def test_postgres_claim_allows_only_one_active_worker(postgres_repo):
    enqueue(postgres_repo, count=4)
    results = parallel(8, lambda _: postgres_repo.claim())
    claimed = [item for item in results if item is not None]
    assert len(claimed) == 1
    with postgres_repo.tx() as session:
        assert session.get(Runtime, 1).active_job == claimed[0].id
        assert session.scalar(select(func.count(Job.id)).where(Job.state == "starting")) == 1
        assert session.scalar(select(func.count(Job.id)).where(Job.state == "queued")) == 3


def test_postgres_expired_attempt_cannot_publish_after_reclaim(postgres_repo):
    enqueue(postgres_repo)
    first = postgres_repo.claim()
    with postgres_repo.tx(lock=True) as session:
        session.get(Job, first.id).lease_until = time.time() - 1
    second = postgres_repo.claim()
    assert second.id == first.id and second.attempt == 2
    assert second.attempt_token != first.attempt_token
    assert not postgres_repo.heartbeat(first.id, first.attempt_token, "generating")
    artifact = {
        "kind": "wav",
        "key": "expiring/test.wav",
        "mime_type": "audio/wav",
        "checksum": "0" * 64,
        "size": 12,
        "duration": 1,
    }
    assert not postgres_repo.complete(first.id, first.attempt_token, [artifact], {})
    assert postgres_repo.complete(second.id, second.attempt_token, [artifact], {"total": 1})
    assert not postgres_repo.complete(second.id, second.attempt_token, [artifact], {"total": 1})
    with postgres_repo.tx() as session:
        assert session.get(Job, first.id).state == "completed"
        assert session.scalar(select(func.count(Artifact.id))) == 1
        assert session.get(Runtime, 1).active_job is None


def test_postgres_idempotency_under_concurrent_submission(postgres_repo, postgres_settings):
    voice_id = add_voice(postgres_repo)
    objects = LocalStorage(postgres_settings.data_dir / "objects")
    request = GenerationRequest(voice_id=voice_id, text="A durable message.")
    results = parallel(8, lambda _: submit(postgres_repo, objects, request, "shared-request"))
    assert len({item.id for item, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    with postgres_repo.tx() as session:
        assert session.scalar(select(func.count(Job.id))) == 1
    changed = GenerationRequest(voice_id=voice_id, text="A different message.")
    with pytest.raises(DomainError) as error:
        submit(postgres_repo, objects, changed, "shared-request")
    assert error.value.code == "idempotency_conflict"


def test_postgres_exhausted_lease_is_terminal(postgres_repo):
    enqueue(postgres_repo)
    for attempt in range(1, postgres_repo.settings.max_attempts + 1):
        claimed = postgres_repo.claim()
        assert claimed.attempt == attempt
        with postgres_repo.tx(lock=True) as session:
            session.get(Job, claimed.id).lease_until = time.time() - 1
    postgres_repo.recover()
    assert postgres_repo.claim() is None
    with postgres_repo.tx() as session:
        item = session.get(Job, claimed.id)
        assert item.state == "failed" and item.error_code == "worker_lost"
        assert session.get(Runtime, 1).active_job is None


def test_encrypted_checkpoint_restores_acknowledged_content_and_revocation(postgres_repo, tmp_path):
    """Actual PG17 custom dump/restore; requires the named disposable Docker test server."""
    import base64
    import subprocess

    from pydantic import SecretStr
    from voice_studio_core.checkpoints import DurabilityCoordinator
    from voice_studio_core.database import Credential
    from voice_studio_core.volume_storage import VolumeStorage

    container = os.getenv("VOICE_TEST_PG_CONTAINER")
    if not container:
        pytest.skip("Set VOICE_TEST_PG_CONTAINER for pg_dump/pg_restore recovery qualification")
    repo = postgres_repo
    storage = VolumeStorage(tmp_path / "persistent-volume")
    source = tmp_path / "object"
    source.write_bytes(b"authorized fixture reference")
    storage.put("voices/test/reference", source)
    source.write_text('{"text":"Acknowledged script"}')
    storage.put("expiring/jobs/test/script.json", source)
    source.write_bytes(b"completed export fixture")
    artifact = storage.put("expiring/jobs/test/result.wav", source)
    with repo.tx() as session:
        schema = session.execute(text("SHOW search_path")).scalar_one()
        voice = Voice(
            name="Restored reference",
            original_key="voices/test/reference",
            reference_key="voices/test/reference",
            checksum="0" * 64,
            details={},
            authorized=True,
            active=True,
        )
        session.add(voice)
        session.flush()
        job = Job(
            voice_id=voice.id,
            options={},
            script_key="expiring/jobs/test/script.json",
            fingerprint="f" * 64,
            idempotency_key="restore-fixture",
            state="completed",
            expires_at=time.time() + 86400,
        )
        session.add(job)
        session.flush()
        session.add(
            Artifact(
                job_id=job.id,
                key="expiring/jobs/test/result.wav",
                kind="wav",
                mime_type="audio/wav",
                duration=1,
                **artifact,
            )
        )
        session.add(Credential(digest="revoked-fixture", kind="api", name="Revoked", revoked=True))
        voice_id, job_id = voice.id, job.id
    repo.settings.backup_key = SecretStr(base64.b64encode(b"r" * 32).decode())
    repo.settings.checkpoint_dir = tmp_path / "checkpoints"
    docker = os.getenv("VOICE_TEST_DOCKER", "docker")

    def dump():
        return subprocess.run(
            [
                docker,
                "exec",
                container,
                "pg_dump",
                "-U",
                "postgres",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--schema=" + schema,
                "postgres",
            ],
            check=True,
            capture_output=True,
        ).stdout

    coordinator = DurabilityCoordinator(repo, storage, dump=dump)
    coordinator.record("acknowledged-operation", "request", {"id": job_id})
    coordinator.checkpoint()
    manifest, backup = coordinator.store.load()
    assert backup.startswith(b"PGDMP")
    assert manifest["revision"] == 1
    with repo.engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    subprocess.run(
        [
            docker,
            "exec",
            "-i",
            container,
            "pg_restore",
            "-U",
            "postgres",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            "-d",
            "postgres",
        ],
        input=backup,
        check=True,
        capture_output=True,
    )
    with repo.tx() as session:
        assert session.get(Voice, voice_id).active
        assert session.get(Job, job_id).state == "completed"
        assert session.scalar(
            select(Credential).where(Credential.digest == "revoked-fixture")
        ).revoked
    for item in manifest["objects"]:
        assert storage.verify(item["key"])["checksum"] == item["checksum"]
    assert coordinator.receipt("acknowledged-operation", "request")["id"] == job_id


@pytest.mark.parametrize("boundary", ["dump", "upload", "after_marker", "lost_response"])
def test_acknowledged_api_key_survives_actual_dump_restore_at_failure_boundaries(
    postgres_repo, tmp_path, monkeypatch, boundary
):
    import base64
    import subprocess

    from fastapi.testclient import TestClient
    from pydantic import SecretStr
    from voice_studio_core.checkpoints import DurabilityCoordinator
    from voice_studio_core.database import Credential
    from voice_studio_core.volume_storage import VolumeStorage
    from voice_studio_service.api import create_app

    container = os.getenv("VOICE_TEST_PG_CONTAINER")
    if not container:
        pytest.skip("Requires disposable PG17 container for failure-injection restore")
    repo = postgres_repo
    config = repo.settings
    config.mode, config.profile = (
        "local",
        "local",
    )  # Exercise API middleware with a loopback fixture credential.
    config.local_token = SecretStr("restore-test-token")
    config.backup_key = SecretStr(base64.b64encode(b"f" * 32).decode())
    config.checkpoint_dir = tmp_path / "checkpoints"
    storage = VolumeStorage(tmp_path / "volume")
    docker = os.getenv("VOICE_TEST_DOCKER", "docker")
    with repo.tx() as session:
        schema = session.execute(text("SHOW search_path")).scalar_one()

    def actual_dump():
        return subprocess.run(
            [
                docker,
                "exec",
                container,
                "pg_dump",
                "-U",
                "postgres",
                "-Fc",
                "--no-owner",
                "--no-acl",
                "--schema=" + schema,
                "postgres",
            ],
            check=True,
            capture_output=True,
        ).stdout

    with TestClient(create_app(config, repo, storage), base_url="http://127.0.0.1:8765") as client:
        coordinator = DurabilityCoordinator(repo, storage, dump=actual_dump)
        repo.durability = coordinator
        coordinator.checkpoint()
        original = coordinator.store.write
        if boundary == "dump":
            coordinator.dump = lambda: (_ for _ in ()).throw(OSError("interrupted dump"))
        elif boundary == "upload":
            coordinator.store.write = lambda *_: (_ for _ in ()).throw(
                OSError("unavailable volume")
            )
        elif boundary == "after_marker":

            def after_marker(*args):
                original(*args)
                raise OSError("lost acknowledgment")

            coordinator.store.write = after_marker
        headers = {
            "Authorization": "Bearer restore-test-token",
            "Idempotency-Key": "durable-create",
        }
        first = client.post("/v1/keys", json={"name": "Restorable key"}, headers=headers)
        assert first.status_code == (200 if boundary == "lost_response" else 503)
        coordinator.dump, coordinator.store.write = actual_dump, original
        acknowledged = client.post("/v1/keys", json={"name": "Restorable key"}, headers=headers)
        assert acknowledged.status_code == 200, acknowledged.text
        restored_key = acknowledged.json()
        identifier = acknowledged.headers["x-voice-operation-id"]
        _, dump = coordinator.store.load()
        with repo.engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        subprocess.run(
            [
                docker,
                "exec",
                "-i",
                container,
                "pg_restore",
                "-U",
                "postgres",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                "-d",
                "postgres",
            ],
            input=dump,
            check=True,
            capture_output=True,
        )
        with repo.tx() as session:
            assert session.get(Credential, restored_key["id"]).name == "Restorable key"
            assert session.scalar(select(func.count(Credential.id))) == 1
        replay = client.post("/v1/keys", json={"name": "Restorable key"}, headers=headers)
        assert replay.json() == restored_key
        assert replay.headers["x-voice-operation-id"] == identifier
