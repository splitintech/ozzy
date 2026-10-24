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
            assert session.execute(text("SELECT version FROM voice_schema")).scalars().all() == [1]
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
