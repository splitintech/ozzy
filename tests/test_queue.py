import queue
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from voice_studio_core.contracts import GenerationRequest
from voice_studio_core.database import Artifact, Job
from voice_studio_service.jobs import submit
from voice_studio_service.worker import EngineProcess, Worker


def new_job(repo, objects, voice, key="job"):
    return submit(
        repo, objects, GenerationRequest(voice_id=voice["id"], text="Test", purpose="audition"), key
    )[0]


def test_concurrent_submission_and_single_claim(repo, objects, voice):
    def call(_):
        return new_job(repo, objects, voice).id

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(call, range(12)))
    assert len(set(ids)) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: repo.claim(), range(4)))
    assert sum(item is not None for item in claims) == 1


def test_expired_lease_fences_previous_attempt(repo, objects, voice):
    new_job(repo, objects, voice)
    first = repo.claim()
    with repo.tx(lock=True) as session:
        session.get(Job, first.id).lease_until = time.time() - 1
    second = repo.claim()
    assert second.attempt == 2 and first.attempt_token != second.attempt_token
    assert repo.complete(first.id, first.attempt_token, [], {}) is False
    assert repo.heartbeat(first.id, first.attempt_token) is False
    assert repo.complete(second.id, second.attempt_token, [], {}) is True


def test_retries_stop_at_limit(repo, objects, voice):
    new_job(repo, objects, voice)
    for _ in range(2):
        job = repo.claim()
        repo.fail(job.id, job.attempt_token, "network", "Storage unavailable", True)
    assert repo.claim() is None
    with repo.tx() as session:
        assert session.get(Job, job.id).state == "failed"


class TestEngine:
    __test__ = False

    def __init__(self, recording):
        self.events = queue.Queue()
        self.recording = recording
        self.stopped = False

    def send(self, payload):
        shutil.copyfile(self.recording, Path(payload["output"]) / "master.wav")
        self.events.put({"state": "generating"})
        self.events.put(
            {
                "event": "completed",
                "metadata": {
                    "duration": 8,
                    "timings": {"generation_seconds": 0.01},
                    "test_fixture": True,
                },
            }
        )

    def stop(self):
        self.stopped = True


def test_worker_commits_only_verified_complete_exports(repo, objects, voice, recording, client):
    job = new_job(repo, objects, voice)
    worker = Worker(repo, objects, TestEngine(recording))
    assert worker.process_one()
    actual = client.get(f"/v1/generations/{job.id}").json()
    assert actual["state"] == "completed", actual
    assert {a["kind"] for a in actual["artifacts"]} == {
        "mp3",
        "wav",
        "telephone",
        "master",
        "manifest",
    }
    for item in actual["artifacts"]:
        assert client.get(item["url"]).status_code == 200
    assert (
        client.post(
            f"/v1/voices/{voice['id']}/activate", json={"audition_job_id": job.id, "approved": True}
        ).status_code
        == 200
    )


def test_upload_failure_never_publishes_success(repo, objects, voice, recording, monkeypatch):
    job = new_job(repo, objects, voice)
    original = objects.put

    def fail(key, *args):
        if key.endswith("speech.mp3"):
            raise OSError("simulated storage failure")
        return original(key, *args)

    monkeypatch.setattr(objects, "put", fail)
    Worker(repo, objects, TestEngine(recording)).process_one()
    with repo.tx() as s:
        assert s.get(Job, job.id).state == "queued"
        assert not list(s.query(Artifact).filter_by(job_id=job.id))
    assert not list(objects.root.rglob("speech.wav"))


def test_running_cancellation_kills_inference_process(repo, objects, voice):
    job = new_job(repo, objects, voice)
    program = "import sys,time,json;sys.stdin.readline();print(json.dumps({'state':'generating'}),flush=True);time.sleep(120)"
    engine = EngineProcess([sys.executable, "-u", "-c", program])
    worker = Worker(repo, objects, engine)
    thread = threading.Thread(target=worker.process_one)
    thread.start()
    deadline = time.time() + 5
    while engine.process is None and time.time() < deadline:
        time.sleep(0.02)
    process = engine.process
    assert process is not None
    repo.cancel(job.id)
    thread.join(timeout=6)
    assert not thread.is_alive()
    assert process.poll() is not None
    with repo.tx() as session:
        assert session.get(Job, job.id).state == "cancelled"


def test_restart_recovers_durable_job(config, repo, objects, voice):
    from voice_studio_core.database import Repository

    job = new_job(repo, objects, voice)
    recovered = Repository(config)
    recovered.initialize()
    try:
        assert recovered.claim().id == job.id
    finally:
        recovered.dispose()


def test_engine_isolation_and_readiness(monkeypatch):
    sensitive_names = ["VOICE_DATABASE_URL", "DAYTONA_API_KEY", "AWS_ACCESS_KEY_ID", "CUSTOM_TOKEN"]
    for name in sensitive_names:
        monkeypatch.setenv(name, "test-secret")
    program = (
        "import json,os,sys;sys.stdin.readline();"
        f"print(json.dumps({{'present':[k for k in {sensitive_names!r} if k in os.environ],"
        "'offline':os.getenv('HF_HUB_OFFLINE')}),flush=True);"
        "print(json.dumps({'state':'generating'}),flush=True);sys.stdin.readline()"
    )
    engine = EngineProcess([sys.executable, "-u", "-c", program])
    assert not engine.ready
    try:
        engine.send({})
        assert engine.events.get(timeout=5) == {"present": [], "offline": "1"}
        assert engine.events.get(timeout=5) == {"state": "generating"}
        assert engine.ready
    finally:
        engine.stop()
    assert not engine.ready


def test_attempt_lease_covers_slow_script_read_and_reference_download(
    config, repo, objects, voice, recording, monkeypatch
):
    config.lease_seconds = 1
    job = new_job(repo, objects, voice)
    original_read, original_download = objects.read, objects.download

    def slow_read(key):
        time.sleep(1.2)  # Longer than a full lease, before inference begins.
        return original_read(key)

    def slow_download(key, destination):
        time.sleep(1.2)
        return original_download(key, destination)

    monkeypatch.setattr(objects, "read", slow_read)
    monkeypatch.setattr(objects, "download", slow_download)
    Worker(repo, objects, TestEngine(recording)).process_one()
    with repo.tx() as session:
        result = session.get(Job, job.id)
        assert result.state == "completed", (result.state, result.error_code)
        assert result.attempt == 1


def test_cancellation_during_reference_download_never_starts_inference(
    repo, objects, voice, recording, monkeypatch
):
    from unittest.mock import Mock

    job = new_job(repo, objects, voice)
    original_download = objects.download

    def cancelled_download(key, destination):
        repo.cancel(job.id)
        return original_download(key, destination)

    monkeypatch.setattr(objects, "download", cancelled_download)
    engine = TestEngine(recording)
    engine.send = Mock()
    Worker(repo, objects, engine).process_one()
    engine.send.assert_not_called()
    with repo.tx() as session:
        result = session.get(Job, job.id)
        assert result.state == "cancelled"
        assert result.error_code == "cancelled"


def test_lost_startup_lease_is_retryable_and_not_owner_cancellation(
    repo, objects, voice, recording, monkeypatch
):
    from unittest.mock import Mock

    job = new_job(repo, objects, voice)
    original_download = objects.download

    def expired_download(key, destination):
        with repo.tx(lock=True) as session:
            session.get(Job, job.id).lease_until = time.time() - 1
        return original_download(key, destination)

    monkeypatch.setattr(objects, "download", expired_download)
    engine = TestEngine(recording)
    engine.send = Mock()
    Worker(repo, objects, engine).process_one()
    engine.send.assert_not_called()
    with repo.tx() as session:
        result = session.get(Job, job.id)
        assert result.state == "queued"
        assert result.error_code == "lease_lost"
        assert result.retryable
        assert not result.cancel_requested


def test_lost_startup_attempt_does_not_modify_replacement_lease(
    repo, objects, voice, recording, monkeypatch
):
    from unittest.mock import Mock

    from voice_studio_core.database import Runtime

    job = new_job(repo, objects, voice)
    original_download = objects.download
    replacement = []

    def replaced_download(key, destination):
        with repo.tx(lock=True) as session:
            session.get(Job, job.id).lease_until = time.time() - 1
        replacement.append(repo.claim())
        return original_download(key, destination)

    monkeypatch.setattr(objects, "download", replaced_download)
    old_engine = TestEngine(recording)
    old_engine.send = Mock()
    Worker(repo, objects, old_engine).process_one()
    old_engine.send.assert_not_called()
    with repo.tx() as session:
        result = session.get(Job, job.id)
        assert result.state == "starting"
        assert result.attempt_token == replacement[0].attempt_token
        assert result.error_code is None
        assert session.get(Runtime, 1).active_job == job.id


def test_cancellation_during_upload_cleans_attempt_objects_without_publishing(
    repo, objects, voice, recording, monkeypatch
):
    job = new_job(repo, objects, voice)
    original_put = objects.put

    def cancelled_upload(key, *args):
        receipt = original_put(key, *args)
        repo.cancel(job.id)
        return receipt

    monkeypatch.setattr(objects, "put", cancelled_upload)
    Worker(repo, objects, TestEngine(recording)).process_one()
    with repo.tx() as session:
        assert session.get(Job, job.id).state == "cancelled"
        assert not list(session.query(Artifact).filter_by(job_id=job.id))
    assert not list(objects.root.rglob("speech.wav"))
