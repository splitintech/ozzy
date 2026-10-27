import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from voice_studio_core.audio import PROCESSING_VERSION, finish_audio
from voice_studio_core.contracts import DomainError
from voice_studio_core.database import Job, Voice


class EngineProcess:
    def __init__(self, command=None):
        self.command = command or (
            [sys.executable, "--engine-runner"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "voice_studio_engine.runner"]
        )
        self.process = None
        self.ready = False
        self.events = queue.Queue()

    def start(self):
        if self.process and self.process.poll() is None:
            return
        self.events = queue.Queue()
        self.ready = False
        child_env = {
            key: value
            for key, value in os.environ.items()
            if not (
                key.startswith(("VOICE_", "DAYTONA_", "AWS_", "S3_"))
                or any(word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
            )
        }
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            env={
                **child_env,
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "PYTHONUNBUFFERED": "1",
            },
        )
        process, events = self.process, self.events

        def read():
            for line in process.stdout:
                try:
                    message = json.loads(line)
                    if self.process is not process:
                        break
                    if message.get("state") == "loading":
                        self.ready = False
                    elif (
                        message.get("state") == "generating" or message.get("event") == "completed"
                    ):
                        self.ready = True
                    elif message.get("event") == "failed":
                        self.ready = False
                    events.put(message)
                except (json.JSONDecodeError, ValueError):
                    continue
            if self.process is process:
                self.ready = False
            events.put({"event": "exited"})

        threading.Thread(target=read, daemon=True).start()

    def send(self, payload):
        self.start()
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def stop(self):
        self.ready = False
        process, self.process = self.process, None
        if process and process.poll() is None:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        capture_output=True,
                        timeout=5,
                    )
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        if process:
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()


class Worker:
    def __init__(self, repo, storage, engine=None):
        self.repo, self.storage = repo, storage
        self.settings = repo.settings
        self.engine = engine or EngineProcess()
        self.stop_event = threading.Event()
        self.last_voice_id = None
        self.last_work = time.monotonic()
        self.last_orphan_cleanup = 0.0

    def process_one(self):
        if self.repo.durability:
            try:
                with self.repo.durability.lock:
                    self.repo.durability.ensure_available()
                    job = self.repo.claim()
            except Exception:
                self.stop_event.wait(1)
                return False
        else:
            job = self.repo.claim()
        if job is None:
            return False
        started = time.monotonic()
        published_keys = []
        complete = False
        phase = "starting"
        attempt_done = threading.Event()
        lease_error = []

        def lease_failure():
            with self.repo.tx() as session:
                current = session.get(Job, job.id)
                if current and (current.cancel_requested or current.deleted):
                    return DomainError("cancelled", "Generation was cancelled", 409)
            return DomainError(
                "lease_lost", "The worker lost its job lease; the request can be retried", 503, True
            )

        def check_attempt(refresh=False):
            if self.stop_event.is_set():
                raise DomainError("worker_stopped", "The worker is shutting down", 503, True)
            if time.monotonic() - started > self.settings.job_timeout_seconds:
                raise DomainError("generation_timeout", "Generation exceeded its time limit", 504)
            if lease_error:
                raise lease_error[0]
            if refresh and not self.repo.heartbeat(job.id, job.attempt_token, phase):
                raise lease_failure()

        def heartbeat():
            interval = max(0.05, min(1.0, self.settings.lease_seconds / 3))
            while not attempt_done.wait(interval):
                try:
                    check_attempt(refresh=True)
                except DomainError as error:
                    lease_error.append(error)
                    return
                except Exception:
                    lease_error.append(
                        DomainError(
                            "lease_unavailable",
                            "The worker could not renew its job lease",
                            503,
                            True,
                        )
                    )
                    return

        # Keep one lease guardian for the entire attempt, including private-object
        # reads and reference downloads before inference, and finishing/upload after.
        keeper = threading.Thread(target=heartbeat, daemon=True)
        keeper.start()
        try:
            with self.repo.tx() as s:
                voice = s.get(Voice, job.voice_id)
                if not voice or voice.deleted:
                    raise DomainError("voice_deleted", "This reference voice was deleted", 409)
            check_attempt(refresh=True)
            script = json.loads(self.storage.read(job.script_key))["text"]
            check_attempt(refresh=True)
            with tempfile.TemporaryDirectory(prefix="job-", dir=self.settings.data_dir) as temp:
                directory = Path(temp)
                reference = directory / "reference.wav"
                self.storage.download(voice.reference_key, reference)
                check_attempt(refresh=True)
                reference_ready_at = time.monotonic()
                output = directory / "output"
                output.mkdir()
                self.last_voice_id = voice.id
                self.engine.send(
                    {
                        "model_dir": str(self.settings.model_dir),
                        "device": self.settings.device,
                        "cpu_threads": self.settings.cpu_threads,
                        "reference": str(reference),
                        "reference_sha256": voice.checksum,
                        "voice_id": voice.id,
                        "text": script,
                        "options": job.options,
                        "output": str(output),
                    }
                )
                ready_at = None
                while True:
                    check_attempt()
                    try:
                        message = self.engine.events.get(timeout=1)
                    except queue.Empty:
                        continue
                    if "state" in message:
                        phase = message["state"]
                        if phase == "generating" and ready_at is None:
                            ready_at = time.monotonic()
                        continue
                    if message.get("event") == "completed":
                        metadata = message["metadata"]
                        break
                    if message.get("event") == "failed":
                        raise DomainError(message["code"], message["message"], 422)
                    if message.get("event") == "exited":
                        raise DomainError(
                            "engine_crashed", "The inference engine stopped unexpectedly", 503, True
                        )
                phase = "processing"
                processing_started = time.monotonic()
                check_attempt(refresh=True)
                exports = finish_audio(output / "master.wav", output)
                metadata["processing_version"] = PROCESSING_VERSION
                metadata["exports"] = [
                    {key: value for key, value in item.items() if key != "path"} for item in exports
                ]
                metadata["attempt"] = job.attempt
                manifest = output / "manifest.json"
                manifest.write_text(json.dumps(metadata, indent=2))
                exports.append(
                    {
                        "kind": "manifest",
                        "path": str(manifest),
                        "mime_type": "application/json",
                        "duration": metadata["duration"],
                    }
                )
                artifacts = []
                upload_started = time.monotonic()
                for item in exports:
                    check_attempt(refresh=True)
                    key = f"expiring/jobs/{job.id}/{job.attempt_token}/{Path(item['path']).name}"
                    receipt = self.storage.put(key, item["path"], item["mime_type"])
                    published_keys.append(key)
                    artifacts.append(
                        {
                            "kind": item["kind"],
                            "key": key,
                            "mime_type": item["mime_type"],
                            "duration": item["duration"],
                            **receipt,
                        }
                    )
                timings = {
                    **metadata["timings"],
                    "queue_wait_seconds": max(0, job.updated_at - job.created_at),
                    "reference_download_seconds": reference_ready_at - started,
                    "processing_seconds": upload_started - processing_started,
                    "upload_seconds": time.monotonic() - upload_started,
                    "total_job_seconds": time.monotonic() - started,
                    "ready_to_artifacts_seconds": (
                        time.monotonic() - ready_at if ready_at else None
                    ),
                }
                check_attempt(refresh=True)
                from .durability import publication
                with publication(self.repo):
                    complete = self.repo.complete(job.id, job.attempt_token, artifacts, timings)
                if not complete:
                    raise lease_failure()
        except DomainError as exc:
            self.engine.stop()
            self.fail(job.id, job.attempt_token, exc.code, exc.message, exc.retryable)
        except Exception:
            self.engine.stop()
            self.fail(
                job.id,
                job.attempt_token,
                "worker_error",
                "The worker could not finish this message.",
                True,
            )
        finally:
            attempt_done.set()
            keeper.join(timeout=6)
            if not complete:
                for key in published_keys:
                    try:
                        self.storage.delete(key)
                    except Exception:
                        pass  # expiring prefix lifecycle and maintenance provide cleanup.
        self.last_work = time.monotonic()
        return True

    def fail(self, *args):
        from .durability import publication
        try:
            with publication(self.repo):
                self.repo.fail(*args)
        except Exception:
            # A pending checkpoint blocks publication; lease recovery retries later.
            pass

    def run(self, exit_when_empty=False):
        try:
            while not self.stop_event.is_set():
                if not self.process_one():
                    if exit_when_empty:
                        return
                    with self.repo.tx() as session:
                        voice = (
                            session.get(Voice, self.last_voice_id) if self.last_voice_id else None
                        )
                        reference_deleted = self.last_voice_id and (not voice or voice.deleted)
                    if (
                        reference_deleted
                        or time.monotonic() - self.last_work
                        > self.settings.local_engine_idle_seconds
                    ):
                        self.engine.stop()
                    from .jobs import maintenance

                    try:
                        if self.repo.durability:
                            # Do not create a checkpoint every idle second; expire only when due.
                            from sqlalchemy import func, select

                            from .durability import publication
                            with self.repo.tx() as s:
                                expired = s.scalar(select(func.count(Job.id)).where(Job.deleted.is_(False), Job.expires_at <= time.time()))
                                expired += s.scalar(select(func.count(Voice.id)).where(Voice.deleted.is_(False), Voice.created_at <= time.time() - 30 * 86400))
                            if expired:
                                with publication(self.repo):
                                    maintenance(self.repo, self.storage)
                            with self.repo.durability.lock:
                                self.repo.durability.ensure_available()
                                maintenance(self.repo, self.storage, True)
                                if time.monotonic() - self.last_orphan_cleanup > 3600:
                                    self.storage.cleanup_orphans([item["key"] for item in (self.repo.durability.last or {}).get("objects", [])])
                                    self.last_orphan_cleanup = time.monotonic()
                        else:
                            maintenance(self.repo, self.storage)
                    except Exception:
                        pass  # Tombstones still prevent reads; cleanup retries next idle cycle.
                    self.stop_event.wait(1)
        finally:
            self.engine.stop()

    def stop(self):
        self.stop_event.set()
        self.engine.stop()
