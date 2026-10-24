"""Daytona control plane. Only the launcher/controller hold provider credentials."""

import hashlib
import json
import os
import shlex
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from daytona import CreateSandboxFromSnapshotParams, Daytona, DaytonaConfig, ListSandboxesQuery
from daytona.common.errors import DaytonaNotFoundError
from daytona.common.process import SessionExecuteRequest
from dotenv import dotenv_values
from sqlalchemy import func, select
from voice_studio_core.contracts import ACTIVE_STATES, DomainError
from voice_studio_core.database import Job, Runtime, uid

from .jobs import maintenance

WORKER_SECRETS = {
    "VOICE_DATABASE_URL",
    "VOICE_S3_BUCKET",
    "VOICE_S3_ENDPOINT",
    "VOICE_S3_REGION",
    "VOICE_S3_ACCESS_KEY",
    "VOICE_S3_SECRET_KEY",
}


def client(settings=None):
    # Launchers need provider credentials only, not the hosted controller's
    # database/password configuration. Read dotenv without mutating process env.
    values = dotenv_values(".env") if settings is None else {}

    def setting(name, default=""):
        return os.environ.get(name, values.get(name) or default)

    key = (
        settings.daytona_api_key.get_secret_value()
        if settings is not None
        else setting("VOICE_DAYTONA_API_KEY")
    )
    if not key:
        key = setting("DAYTONA_API_KEY")
    if not key:
        raise DomainError(
            "daytona_credentials", "Configure your Daytona API key in the launcher", 503
        )
    return Daytona(
        DaytonaConfig(
            api_key=key,
            api_url=(
                settings.daytona_api_url
                if settings is not None
                else setting("VOICE_DAYTONA_API_URL", "https://app.daytona.io/api")
            ),
        )
    )


def state_name(sandbox):
    state = sandbox.state
    return str(getattr(state, "value", state)).lower()


def launch_process(sandbox, role):
    # Only a constant role and provider-generated identifier enter the shell command.
    if role not in {"serve", "worker"}:
        raise ValueError("Unknown application process role")
    session = f"voice-{role}"
    try:
        sandbox.process.delete_session(session)
    except Exception:
        pass
    sandbox.process.create_session(session)
    result = sandbox.process.execute_session_command(
        session,
        SessionExecuteRequest(
            command=f"VOICE_SANDBOX_ID={shlex.quote(sandbox.id)} /opt/voice/.venv/bin/voice {role}",
            run_async=True,
            suppress_input_echo=True,
        ),
    )
    return result.cmd_id


def launch(snapshot, secret_names, deployment="voice-studio", sdk=None):
    if not snapshot:
        raise DomainError("snapshot_required", "Configure a qualified controller snapshot", 400)
    sdk = sdk or client()
    candidates = list(
        sdk.list(
            ListSandboxesQuery(
                labels={"app": "voice-studio", "deployment": deployment, "role": "controller"}
            )
        )
    )
    running = [s for s in candidates if state_name(s) == "started"]
    if len(running) > 1:
        raise DomainError(
            "duplicate_controllers",
            "Multiple controllers exist. Stop this deployment before restarting.",
            409,
        )
    if running:
        sandbox = running[0]
        sandbox.refresh_activity()
        sandbox.set_ttl(120)
    else:
        # A new controller recovers the database and reconciles old worker sandboxes.
        # Stopped controllers contain no authoritative state and are safely replaceable.
        for old in candidates:
            if state_name(old) not in {"stopped", "archived", "error"}:
                raise DomainError(
                    "session_transition",
                    "The hosted session is still changing state. Try status shortly.",
                    409,
                )
            sdk.delete(old, wait=True)
        launch_token = uid()
        sandbox = None
        try:
            sandbox = sdk.create(
                CreateSandboxFromSnapshotParams(
                    name="voice-controller-" + hashlib.sha256(deployment.encode()).hexdigest()[:24],
                    snapshot=snapshot,
                    language="python",
                    public=False,
                    labels={
                        "app": "voice-studio",
                        "deployment": deployment,
                        "role": "controller",
                        "voice-started-at": str(time.time()),
                        "voice-launch": launch_token,
                    },
                    env_vars={
                        "VOICE_MODE": "hosted",
                        "VOICE_ROLE": "controller",
                        "VOICE_BIND_HOST": "0.0.0.0",
                        "VOICE_DEPLOYMENT_ID": deployment,
                        "VOICE_DAYTONA_SECRET_NAMES": json.dumps(secret_names),
                    },
                    secrets=secret_names,
                    auto_stop_interval=15,
                    auto_archive_interval=60,
                    auto_delete_interval=1440,
                    ttl_minutes=120,
                ),
                timeout=180,
            )
            launch_process(sandbox, "serve")
        except Exception as error:
            # A timed-out create may have allocated a resource despite returning no
            # ID. Only clean up this launch token; never delete a racing launcher.
            try:
                allocated = (
                    [sandbox]
                    if sandbox
                    else list(
                        sdk.list(
                            ListSandboxesQuery(
                                labels={
                                    "app": "voice-studio",
                                    "deployment": deployment,
                                    "role": "controller",
                                    "voice-launch": launch_token,
                                }
                            )
                        )
                    )
                )
                for created in allocated:
                    try:
                        sdk.delete(created, timeout=120, wait=True)
                    except Exception as cleanup_error:
                        if not missing(cleanup_error):
                            raise
            except Exception as cleanup_error:
                raise DomainError(
                    "controller_cleanup_incomplete",
                    "Controller startup failed and cleanup is uncertain. Inspect Daytona before retrying.",
                    503,
                ) from cleanup_error
            raise error
    signed = sandbox.create_signed_preview_url(8765, expires_in_seconds=3600)
    return {
        "sandbox_id": sandbox.id,
        "url": signed.url,
        "expires_at": time.time() + 3600,
        "deployment": deployment,
        "state": state_name(sandbox),
    }


def status(deployment="voice-studio", sdk=None):
    sdk = sdk or client()
    return [
        {"sandbox_id": s.id, "state": state_name(s), "role": s.labels.get("role")}
        for s in sdk.list(
            ListSandboxesQuery(labels={"app": "voice-studio", "deployment": deployment})
        )
    ]


def missing(error):
    """Only an actual provider 404 proves a sandbox no longer exists."""
    return isinstance(error, DaytonaNotFoundError) and error.status_code == 404


def stop(deployment="voice-studio", sdk=None):
    sdk = sdk or client()
    sandboxes = list(
        sdk.list(ListSandboxesQuery(labels={"app": "voice-studio", "deployment": deployment}))
    )
    failures = []
    # Stop schedulers first. Deleting workers while their controller still runs can
    # race another dispatch and leave a replacement behind after the stop returns.
    controllers = [s for s in sandboxes if s.labels.get("role") == "controller"]
    for sandbox in controllers:
        try:
            if state_name(sandbox) == "started":
                sandbox.stop(timeout=120)
            elif state_name(sandbox) not in {"stopped", "archived", "destroyed"}:
                sdk.delete(sandbox, timeout=120, wait=True)
        except Exception as error:
            if not missing(error):
                failures.append(sandbox.id)
    # Re-list after scheduler shutdown to include a dispatch that completed during
    # shutdown. A list failure must propagate; the caller cannot claim cleanup.
    remaining = list(
        sdk.list(ListSandboxesQuery(labels={"app": "voice-studio", "deployment": deployment}))
    )
    for sandbox in remaining:
        if sandbox.labels.get("role") == "controller":
            continue
        try:
            # Workers have no durable local data. GPU stop is destructive in Daytona;
            # delete explicitly so that stopped workers cannot become orphan resources.
            if sandbox.labels.get("role") == "worker" or sandbox.gpu:
                sdk.delete(sandbox, timeout=120, wait=True)
            elif state_name(sandbox) == "started":
                sandbox.stop(timeout=120)
        except Exception as error:
            if not missing(error):
                failures.append(sandbox.id)
    if failures:
        raise DomainError(
            "stop_incomplete",
            "Some sandboxes did not stop; inspect Daytona before closing the launcher",
            503,
        )
    return {"stopped": len({s.id for s in sandboxes + remaining})}


def started_at(sandbox, fallback):
    """The launcher timestamp includes allocation/startup, even after a restart."""
    try:
        return min(float(sandbox.labels["voice-started-at"]), fallback)
    except (KeyError, TypeError, ValueError):
        try:
            value = sandbox.created_at
            if isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return min(value.timestamp(), fallback)
        except (AttributeError, TypeError, ValueError):
            return fallback


def validate_worker_profile(resource, settings, snapshot=False):
    expected = {
        "cpu": settings.worker_cpu,
        "mem" if snapshot else "memory": settings.worker_memory_gib,
        "disk": settings.worker_disk_gib,
    }
    mismatch = [key for key, value in expected.items() if getattr(resource, key, None) != value]
    gpu = getattr(resource, "gpu", None)
    if gpu != (1 if settings.device == "cuda" else 0):
        mismatch.append("gpu")
    actual_type = getattr(resource, "gpu_type", None)
    actual_type = getattr(actual_type, "value", actual_type)
    if settings.worker_gpu_type and actual_type != settings.worker_gpu_type:
        mismatch.append("gpu_type")
    if mismatch:
        raise DomainError(
            "worker_profile_mismatch",
            "Worker snapshot/resources do not match the configured profile: " + ", ".join(mismatch),
            503,
        )


@dataclass
class Dispatch:
    sandbox: object
    command_id: str | None
    started_at: float
    job_id: str | None = None
    attempt: int | None = None


class Controller:
    def __init__(self, settings, repo, storage, sdk=None):
        self.settings, self.repo, self.storage = settings, repo, storage
        self.sdk = sdk
        self.exit = threading.Event()
        self.dispatch = None
        self.last_error = None
        self.thread = None
        self.started = time.time()
        self.last_maintenance = 0

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.exit.set()
        if self.thread:
            self.thread.join(timeout=15)

    def workers(self):
        return self.sdk.list(
            ListSandboxesQuery(
                labels={
                    "app": "voice-studio",
                    "deployment": self.settings.deployment_id,
                    "role": "worker",
                }
            )
        )

    def charge(self, sandbox, role, fallback):
        self.repo.charge(
            f"sandbox:{sandbox.id}",
            "inference" if role == "worker" else "controller",
            time.time() - started_at(sandbox, fallback),
            self.settings.worker_hourly_usd
            if role == "worker"
            else self.settings.controller_hourly_usd,
        )

    def delete_worker(self, sandbox, fallback):
        # Upsert before and after teardown. A failed delete remains tracked and is
        # retried before any replacement is created. Never hide uncertain failures.
        self.charge(sandbox, "worker", fallback)
        try:
            self.sdk.delete(sandbox, timeout=120, wait=True)
        except Exception as error:
            if not missing(error):
                raise
        self.charge(sandbox, "worker", fallback)

    def clear_dispatch(self, job_id=None, attempt=None, lost=True):
        with self.repo.tx(lock=True) as s:
            runtime = s.get(Runtime, 1)
            # Failed provisioning/process launch consumes the existing attempt budget;
            # successful worker claims already increment it and must not be double counted.
            job = s.get(Job, job_id) if job_id else None
            if job and job.state == "queued" and (attempt is None or job.attempt == attempt):
                job.attempt += 1
                job.error_code = "worker_launch_failed"
                job.error_message = "The worker could not start. Check the hosted configuration."
                job.retryable = True
                job.updated_at = time.time()
                if job.attempt >= self.settings.max_attempts:
                    job.state = "failed"
            if lost and runtime.active_job:
                active = s.get(Job, runtime.active_job)
                if active and active.state in ACTIVE_STATES:
                    active.lease_until = 0
            runtime.worker_sandbox_id = None
            runtime.dispatch_token = None
            runtime.dispatch_lease_until = None
        self.repo.recover()

    def reconcile(self):
        # First prove every old worker is gone. Only then fence leases/retry work.
        with self.repo.tx() as s:
            runtime = s.get(Runtime, 1)
            token = runtime.dispatch_token
            incomplete_launch = bool(runtime.dispatch_lease_until)
        fields = token.split(":") if token else []
        launch_job = fields[0] if fields and (len(fields) == 3 or incomplete_launch) else None
        launch_attempt = int(fields[1]) if len(fields) == 3 else None
        for sandbox in list(self.workers()):
            labels = sandbox.labels
            if labels.get("voice-job-id") and labels.get("voice-attempt") is not None:
                launch_job = labels["voice-job-id"]
                launch_attempt = int(labels["voice-attempt"])
            self.delete_worker(sandbox, self.started)
        self.clear_dispatch(launch_job, launch_attempt)
        self.dispatch = None

    def retire_dispatch(self):
        dispatch = self.dispatch
        self.delete_worker(dispatch.sandbox, dispatch.started_at)
        self.clear_dispatch(dispatch.job_id, dispatch.attempt)
        self.dispatch = None

    def start_worker(self):
        now = time.time()
        with self.repo.tx(lock=True) as s:
            runtime = s.get(Runtime, 1)
            if runtime.dispatch_token or runtime.active_job:
                return
            job = s.scalar(
                select(Job)
                .where(
                    Job.state == "queued", Job.deleted.is_(False), Job.cancel_requested.is_(False)
                )
                .order_by(Job.created_at)
                .limit(1)
            )
            if not job:
                return
            # Encode the intended job in the durable reservation without a schema change.
            token = f"{job.id}:{job.attempt}:{uid()[:20]}"
            job_id, attempt = job.id, job.attempt
            runtime.dispatch_token = token
            runtime.dispatch_lease_until = now + 240
        try:
            if not self.settings.daytona_worker_snapshot:
                raise DomainError(
                    "worker_snapshot_missing",
                    "Configure the worker snapshot before generating",
                    503,
                )
            validate_worker_profile(
                self.sdk.snapshot.get(self.settings.daytona_worker_snapshot),
                self.settings,
                snapshot=True,
            )
            sandbox = self.sdk.create(
                CreateSandboxFromSnapshotParams(
                    # Stable provider name also prevents duplicate allocation when a
                    # timed-out create completes after the reconciliation list call.
                    name="voice-worker-"
                    + hashlib.sha256(self.settings.deployment_id.encode()).hexdigest()[:24],
                    snapshot=self.settings.daytona_worker_snapshot,
                    language="python",
                    public=False,
                    labels={
                        "app": "voice-studio",
                        "deployment": self.settings.deployment_id,
                        "role": "worker",
                        "voice-dispatch": token,
                        "voice-job-id": job_id,
                        "voice-attempt": str(attempt),
                        "voice-started-at": str(now),
                    },
                    env_vars={
                        "VOICE_MODE": "hosted",
                        "VOICE_ROLE": "worker",
                        "VOICE_DEVICE": self.settings.device,
                        "VOICE_CPU_THREADS": str(self.settings.worker_cpu),
                        "VOICE_DEPLOYMENT_ID": self.settings.deployment_id,
                        "VOICE_DATA_DIR": "/tmp/voice-data",
                        "VOICE_MODEL_DIR": "/opt/voice/models",
                        "VOICE_HOSTED_MODELS": json.dumps(self.settings.hosted_models),
                        "VOICE_MAX_ATTEMPTS": str(self.settings.max_attempts),
                        "VOICE_LEASE_SECONDS": str(self.settings.lease_seconds),
                        "VOICE_JOB_TIMEOUT_SECONDS": str(self.settings.job_timeout_seconds),
                    },
                    secrets={
                        k: v
                        for k, v in self.settings.daytona_secret_names.items()
                        if k in WORKER_SECRETS
                    },
                    auto_stop_interval=5,
                    auto_delete_interval=0,
                    ttl_minutes=60,
                ),
                timeout=180,
            )
            self.dispatch = Dispatch(sandbox, None, now, job_id, attempt)
            # Record ownership before launching; a crash is recoverable by labels + DB.
            with self.repo.tx(lock=True) as s:
                s.get(Runtime, 1).worker_sandbox_id = sandbox.id
            validate_worker_profile(sandbox, self.settings)
            self.dispatch.command_id = launch_process(sandbox, "worker")
            with self.repo.tx(lock=True) as s:
                s.get(Runtime, 1).dispatch_lease_until = None
            self.charge(sandbox, "worker", now)
        except Exception:
            if self.dispatch:
                self.retire_dispatch()
            else:
                # A create timeout can still allocate a sandbox server-side. Discover
                # and delete it before clearing the durable launch reservation.
                self.reconcile()
            raise

    def keep_controller(self, pending, idle):
        if not self.settings.sandbox_id:
            return
        own = self.sdk.get(self.settings.sandbox_id)
        self.charge(own, "controller", self.started)
        if pending or self.dispatch or idle < self.settings.controller_idle_seconds:
            own.refresh_activity()
            own.set_ttl(120)
        else:
            # Owner activity is updated by explicit authenticated actions, not polling
            # or worker heartbeats. Active jobs always override the idle timer.
            own.stop(timeout=60)
            self.charge(own, "controller", self.started)
            self.exit.set()

    def tick(self):
        self.repo.recover()
        now = time.time()
        if now - self.last_maintenance > 60:
            maintenance(self.repo, self.storage)
            self.last_maintenance = now
        with self.repo.tx() as s:
            runtime = s.get(Runtime, 1)
            pending = s.scalar(
                select(func.count(Job.id)).where(Job.state.in_(["queued", *ACTIVE_STATES]))
            )
            active = bool(runtime.active_job)
            idle = now - runtime.last_activity
            stale_dispatch = bool(runtime.dispatch_token or runtime.worker_sandbox_id)
        # Keep the controller alive even if a later provider call fails or is slow.
        self.keep_controller(pending or stale_dispatch, idle)
        if self.exit.is_set():
            return
        if self.dispatch:
            dispatch = self.dispatch
            try:
                dispatch.sandbox = self.sdk.get(dispatch.sandbox.id)
            except Exception as error:
                if not missing(error):
                    raise
                self.retire_dispatch()
                return
            state = state_name(dispatch.sandbox)
            if state in {"stopped", "archived", "error", "destroyed"}:
                self.retire_dispatch()
            elif state == "started":
                dispatch.sandbox.refresh_activity()
                dispatch.sandbox.set_ttl(60)
                self.charge(dispatch.sandbox, "worker", dispatch.started_at)
                # A session can remain alive without ever claiming work (e.g. a
                # hung bootstrap/database connection). Do not renew its TTL forever.
                unclaimed_timeout = not active and now - dispatch.started_at >= 240
                if not dispatch.command_id or not pending or unclaimed_timeout:
                    self.retire_dispatch()
                else:
                    try:
                        command = dispatch.sandbox.process.get_session_command(
                            "voice-worker", dispatch.command_id
                        )
                    except Exception as error:
                        if not missing(error):
                            raise
                        self.retire_dispatch()
                    else:
                        if command.exit_code is not None:
                            self.retire_dispatch()
        elif stale_dispatch:
            self.reconcile()
        elif pending and not active:
            # Recheck the deployment label before creating a replacement, including
            # resources left by a create request whose response was lost.
            self.reconcile()
            self.start_worker()

    def elect(self):
        with self.repo.tx(lock=True) as s:
            runtime = s.get(Runtime, 1)
            previous = runtime.controller_sandbox_id
            if previous and previous != self.settings.sandbox_id:
                try:
                    old = self.sdk.get(previous)
                except Exception as error:
                    if not missing(error):
                        raise DomainError(
                            "controller_unverified", "Unable to verify the previous controller", 503
                        ) from error
                else:
                    if state_name(old) not in {"stopped", "archived", "error", "destroyed"}:
                        raise DomainError(
                            "controller_owned", "Another controller owns this deployment", 409
                        )
                    self.sdk.delete(old, timeout=120, wait=True)
            runtime.controller_sandbox_id = self.settings.sandbox_id

    def run(self):
        elected = reconciled = False
        while not self.exit.is_set():
            try:
                self.sdk = self.sdk or client(self.settings)
                if not elected:
                    self.elect()
                    elected = True
                if not reconciled:
                    self.reconcile()
                    reconciled = True
                self.tick()
                self.last_error = None
            except DomainError as error:
                self.last_error = error.message
                if error.code == "controller_owned" and self.settings.sandbox_id:
                    # The durable election chooses one controller. A loser only
                    # deletes itself and must never touch the owner's workers.
                    self.exit.set()
                    try:
                        self.sdk.delete(
                            self.sdk.get(self.settings.sandbox_id), timeout=120, wait=True
                        )
                    except Exception:
                        self.last_error += "; duplicate controller cleanup needs attention"
            except Exception:
                self.last_error = (
                    "Daytona orchestration failed; check quota, configuration, and provider status"
                )
            # Election, credentials and provider discovery may fail transiently.
            # Retry in the same process; the provider TTL remains the finite backstop.
            self.exit.wait(10)
