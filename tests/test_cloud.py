"""Provider-free fault injection using the installed Daytona SDK request/error types."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from daytona.common.errors import DaytonaError, DaytonaNotFoundError
from sqlalchemy import select
from voice_studio_core.contracts import DomainError
from voice_studio_core.database import Charge, Job, Runtime, Voice
from voice_studio_service import cloud


def absent():
    return DaytonaNotFoundError("No such sandbox", status_code=404)


class Sandbox:
    def __init__(self, sandbox_id, role="worker", state="started", labels=None):
        self.id, self.state = sandbox_id, state
        self.labels = {
            "app": "voice-studio",
            "deployment": "voice-studio",
            "role": role,
            **(labels or {}),
        }
        self.cpu, self.memory, self.disk, self.gpu, self.gpu_type = 4, 8, 10, 0, None
        self.created_at = None
        self.refresh_activity, self.set_ttl, self.stop = Mock(), Mock(), Mock()
        self.create_signed_preview_url = Mock(
            return_value=SimpleNamespace(url="https://private.test")
        )
        self.process = SimpleNamespace(
            delete_session=Mock(),
            create_session=Mock(),
            execute_session_command=Mock(return_value=SimpleNamespace(cmd_id="command-1")),
            get_session_command=Mock(return_value=SimpleNamespace(exit_code=None)),
        )


class Daytona:
    def __init__(self, *sandboxes):
        self.sandboxes = {s.id: s for s in sandboxes}
        self.created, self.deleted = [], []
        self.get_errors = {}
        self.create_error = None
        self.delete_error = None
        self.on_create = None
        self.on_delete = None
        self.snapshot = SimpleNamespace(
            get=Mock(return_value=SimpleNamespace(cpu=4, mem=8, disk=10, gpu=0, gpu_type=None))
        )

    def get(self, sandbox_id):
        if sandbox_id in self.get_errors:
            raise self.get_errors[sandbox_id]
        if sandbox_id not in self.sandboxes:
            raise absent()
        return self.sandboxes[sandbox_id]

    def list(self, query):
        return iter(
            [
                s
                for s in self.sandboxes.values()
                if all(s.labels.get(k) == v for k, v in query.labels.items())
            ]
        )

    def create(self, params, timeout):
        self.created.append(params)
        if self.create_error:
            raise self.create_error
        sandbox = Sandbox(f"worker-{len(self.created)}", labels=params.labels)
        self.sandboxes[sandbox.id] = sandbox
        if self.on_create:
            self.on_create(sandbox)
        return sandbox

    def delete(self, sandbox, timeout=60, wait=False):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(sandbox.id)
        if self.on_delete:
            self.on_delete(sandbox)
        self.sandboxes.pop(sandbox.id, None)


@pytest.fixture
def queued(repo):
    with repo.tx(lock=True) as session:
        voice = Voice(
            id="voice",
            name="Test",
            original_key="test",
            reference_key="test",
            checksum="0" * 64,
            details={},
            authorized=True,
            active=True,
        )
        session.add(voice)
        session.flush()
        job = Job(
            id="job-1",
            voice_id=voice.id,
            options={},
            script_key="script",
            fingerprint="0" * 64,
            idempotency_key="cloud-test",
        )
        session.add(job)
    return job.id


@pytest.fixture
def controller(config, repo, objects):
    config.daytona_worker_snapshot = "qualified-worker"
    return cloud.Controller(config, repo, objects, sdk=Daytona())


def runtime(repo):
    with repo.tx() as session:
        return session.get(Runtime, 1)


def job(repo, job_id):
    with repo.tx() as session:
        return session.get(Job, job_id)


def test_recreated_controller_replaces_confirmed_missing_owner(controller, repo, config):
    config.sandbox_id = "new-controller"
    with repo.tx(lock=True) as session:
        session.get(Runtime, 1).controller_sandbox_id = "deleted-controller"
    controller.elect()
    assert runtime(repo).controller_sandbox_id == "new-controller"


@pytest.mark.parametrize(
    "error",
    [
        DaytonaError("Unavailable", status_code=503),
        DaytonaError("Forbidden", status_code=403),
        DaytonaNotFoundError("Unconfirmed missing"),
    ],
)
def test_controller_election_fails_closed_on_uncertain_provider_errors(
    controller, repo, config, error
):
    config.sandbox_id = "new-controller"
    with repo.tx(lock=True) as session:
        session.get(Runtime, 1).controller_sandbox_id = "old-controller"
    controller.sdk.get_errors["old-controller"] = error
    with pytest.raises(DomainError, match="Unable to verify"):
        controller.elect()
    assert runtime(repo).controller_sandbox_id == "old-controller"


@pytest.mark.parametrize("state", ["started", "starting", "stopping", "paused"])
def test_existing_or_transitioning_controller_retains_ownership(controller, repo, config, state):
    config.sandbox_id = "new-controller"
    old = Sandbox("old-controller", "controller", state)
    controller.sdk.sandboxes[old.id] = old
    with repo.tx(lock=True) as session:
        session.get(Runtime, 1).controller_sandbox_id = old.id
    with pytest.raises(DomainError, match="Another controller"):
        controller.elect()
    assert runtime(repo).controller_sandbox_id == old.id


def test_failed_create_consumes_durable_bounded_attempts(controller, repo, queued):
    controller.sdk.create_error = DaytonaError("Capacity unavailable", status_code=503)
    for expected in range(1, repo.settings.max_attempts + 1):
        with pytest.raises(DaytonaError):
            controller.tick()
        assert job(repo, queued).attempt == expected
        assert runtime(repo).dispatch_token is None
    assert job(repo, queued).state == "failed"
    controller.tick()
    assert len(controller.sdk.created) == repo.settings.max_attempts


def test_process_launch_failure_deletes_sandbox_before_retry(controller, repo, queued):
    def fail_launch(sandbox):
        sandbox.process.execute_session_command.side_effect = DaytonaError("Daemon unavailable")

    controller.sdk.on_create = fail_launch
    with pytest.raises(DaytonaError):
        controller.tick()
    assert controller.sdk.deleted == ["worker-1"]
    assert controller.dispatch is None
    assert runtime(repo).worker_sandbox_id is None
    assert job(repo, queued).attempt == 1


def test_failed_cleanup_retains_ownership_and_blocks_replacement(controller, repo, queued):
    def fail_launch(sandbox):
        sandbox.process.execute_session_command.side_effect = DaytonaError("Daemon unavailable")

    controller.sdk.on_create = fail_launch
    controller.sdk.delete_error = DaytonaError("Delete unavailable", status_code=503)
    with pytest.raises(DaytonaError):
        controller.tick()
    assert controller.dispatch is not None
    assert runtime(repo).dispatch_token
    with pytest.raises(DaytonaError):
        controller.tick()
    assert len(controller.sdk.created) == 1
    controller.sdk.delete_error = None
    controller.tick()
    assert controller.dispatch is None
    assert job(repo, queued).attempt == 1


@pytest.mark.parametrize("state", ["deleted", "stopped", "archived", "error"])
def test_lost_worker_fences_attempt_then_allows_recovery(controller, repo, queued, state):
    controller.tick()
    claimed = repo.claim()
    sandbox = controller.dispatch.sandbox
    if state == "deleted":
        controller.sdk.sandboxes.pop(sandbox.id)
    else:
        sandbox.state = state
    controller.tick()
    assert controller.dispatch is None
    assert job(repo, queued).state == "queued"
    assert not repo.heartbeat(queued, claimed.attempt_token)
    assert not repo.complete(queued, claimed.attempt_token, [], {})
    controller.tick()
    assert len(controller.sdk.created) == 2
    assert repo.claim().attempt == 2


def test_worker_probe_outage_does_not_fence_live_work(controller, repo, queued):
    controller.tick()
    claimed = repo.claim()
    controller.sdk.get_errors[controller.dispatch.sandbox.id] = DaytonaError("Transient", 503)
    with pytest.raises(DaytonaError):
        controller.tick()
    assert controller.dispatch is not None
    assert repo.heartbeat(queued, claimed.attempt_token)
    assert not controller.sdk.deleted


def test_missing_process_is_deleted_and_counts_unclaimed_launch(controller, repo, queued):
    controller.tick()
    controller.dispatch.sandbox.process.get_session_command.side_effect = absent()
    controller.tick()
    assert controller.dispatch is None
    assert job(repo, queued).attempt == 1
    assert controller.sdk.deleted == ["worker-1"]


def test_reconciliation_deletes_orphans_before_lease_recovery(controller, repo, queued):
    controller.tick()
    claimed = repo.claim()
    orphan = Sandbox("orphan")
    controller.sdk.sandboxes[orphan.id] = orphan
    replacement = cloud.Controller(controller.settings, repo, controller.storage, controller.sdk)
    replacement.reconcile()
    assert set(controller.sdk.deleted) == {"worker-1", "orphan"}
    assert runtime(repo).worker_sandbox_id is None
    assert job(repo, queued).state == "queued"
    assert not repo.heartbeat(queued, claimed.attempt_token)
    replacement.tick()
    assert len(controller.sdk.created) == 2


def test_restart_recovers_unclaimed_worker_deleted_outside_controller(controller, repo, queued):
    controller.tick()
    controller.sdk.sandboxes.clear()
    replacement = cloud.Controller(controller.settings, repo, controller.storage, controller.sdk)
    replacement.reconcile()
    assert job(repo, queued).attempt == 1
    assert runtime(repo).dispatch_token is None


def test_lease_reservation_survives_crash_before_create(controller, repo, queued):
    with repo.tx(lock=True) as session:
        item = session.get(Runtime, 1)
        item.dispatch_token = f"{queued}:0:reservation"
        item.dispatch_lease_until = time.time() + 240
    controller.reconcile()
    assert job(repo, queued).attempt == 1
    assert runtime(repo).dispatch_token is None


def test_active_work_refreshes_both_ttls_after_owner_idle(controller, repo, config, queued):
    config.sandbox_id = "controller"
    own = Sandbox(config.sandbox_id, "controller")
    controller.sdk.sandboxes[own.id] = own
    with repo.tx(lock=True) as session:
        session.get(Runtime, 1).last_activity = time.time() - 3600
    previous_activity = runtime(repo).last_activity
    controller.tick()
    repo.claim()
    controller.tick()
    own.stop.assert_not_called()
    own.set_ttl.assert_called_with(120)
    worker = controller.dispatch.sandbox
    worker.refresh_activity.assert_called_once()
    worker.set_ttl.assert_called_with(60)
    assert runtime(repo).last_activity == previous_activity


def test_empty_worker_is_deleted_then_idle_controller_stops(controller, repo, config, queued):
    config.sandbox_id = "controller"
    own = Sandbox(config.sandbox_id, "controller")
    controller.sdk.sandboxes[own.id] = own
    with repo.tx(lock=True) as session:
        session.get(Runtime, 1).last_activity = time.time() - 601
    controller.tick()
    claimed = repo.claim()
    repo.complete(queued, claimed.attempt_token, [], {})
    controller.tick()
    assert controller.sdk.deleted == ["worker-1"]
    own.stop.assert_not_called()
    controller.tick()
    own.stop.assert_called_once_with(timeout=60)
    assert controller.exit.is_set()


def test_polling_does_not_reset_owner_idle_deadline(client, repo):
    previous_activity = runtime(repo).last_activity
    for path in ["/health", "/v1/capabilities", "/v1/generations", "/v1/voices"]:
        assert client.get(path).status_code == 200
    assert runtime(repo).last_activity == previous_activity


def test_worker_receives_only_data_credentials(controller, config, queued):
    config.daytona_secret_names = {
        "VOICE_DATABASE_URL": "database-secret",
        "VOICE_S3_BUCKET": "bucket-secret",
        "VOICE_S3_ACCESS_KEY": "access-secret",
        "VOICE_S3_SECRET_KEY": "s3-secret",
        "VOICE_DAYTONA_API_KEY": "provider-secret",
        "DAYTONA_API_KEY": "provider-fallback",
        "VOICE_OWNER_PASSWORD_HASH": "owner-secret",
        "VOICE_SESSION_SECRET": "session-secret",
    }
    controller.tick()
    params = controller.sdk.created[0]
    assert set(params.secrets) == {
        "VOICE_DATABASE_URL",
        "VOICE_S3_BUCKET",
        "VOICE_S3_ACCESS_KEY",
        "VOICE_S3_SECRET_KEY",
    }
    assert not (set(params.env_vars) & {"VOICE_DAYTONA_API_KEY", "VOICE_OWNER_PASSWORD_HASH"})
    assert params.auto_stop_interval == 5 and params.ttl_minutes == 60


@pytest.mark.parametrize("kind", ["snapshot", "sandbox"])
def test_resource_mismatch_blocks_inference_and_cleans_allocation(controller, repo, queued, kind):
    if kind == "snapshot":
        controller.sdk.snapshot.get.return_value.mem = 2
    else:
        controller.sdk.on_create = lambda sandbox: setattr(sandbox, "memory", 2)
    with pytest.raises(DomainError, match="configured profile"):
        controller.tick()
    assert job(repo, queued).attempt == 1
    if kind == "snapshot":
        assert not controller.sdk.created
    else:
        assert controller.sdk.deleted == ["worker-1"]


def test_gpu_type_and_quantity_are_checked(controller, config):
    config.device, config.worker_gpu_type = "cuda", "l4"
    profile = SimpleNamespace(cpu=4, mem=8, disk=10, gpu=1, gpu_type="l4")
    cloud.validate_worker_profile(profile, config, snapshot=True)
    profile.gpu_type = "a100"
    with pytest.raises(DomainError, match="gpu_type"):
        cloud.validate_worker_profile(profile, config, snapshot=True)
    profile.gpu_type, profile.gpu = "l4", 0
    with pytest.raises(DomainError, match="gpu"):
        cloud.validate_worker_profile(profile, config, snapshot=True)


def test_sandbox_cost_includes_startup_and_teardown_without_duplicate_charges(
    controller, repo, queued, monkeypatch
):
    clock = [time.time()]
    monkeypatch.setattr(cloud.time, "time", lambda: clock[0])
    controller.sdk.on_create = lambda _: clock.__setitem__(0, clock[0] + 15)
    controller.sdk.on_delete = lambda _: clock.__setitem__(0, clock[0] + 5)
    controller.tick()
    claimed = repo.claim()
    repo.complete(queued, claimed.attempt_token, [], {})
    clock[0] += 20
    controller.tick()
    with repo.tx() as session:
        charges = list(session.scalars(select(Charge)))
    assert len(charges) == 1
    assert charges[0].seconds == pytest.approx(40)
    assert charges[0].amount_usd == pytest.approx(40 * controller.settings.worker_hourly_usd / 3600)


def test_explicit_stop_deletes_worker_including_gpu_and_stops_controller():
    worker, own = Sandbox("gpu-worker"), Sandbox("controller", "controller")
    worker.gpu = 1
    sdk = Daytona(worker, own)
    assert cloud.stop(sdk=sdk) == {"stopped": 2}
    assert sdk.deleted == [worker.id]
    worker.stop.assert_not_called()
    own.stop.assert_called_once_with(timeout=120)


def test_failed_controller_process_launch_is_cleaned_up():
    sdk = Daytona()
    sdk.on_create = lambda s: setattr(
        s.process.execute_session_command, "side_effect", DaytonaError("Launch failed")
    )
    with pytest.raises(DaytonaError):
        cloud.launch("controller-snapshot", {}, sdk=sdk)
    assert sdk.deleted == ["worker-1"]


def test_create_timeout_reconciles_allocation_with_lost_response(controller, repo, queued):
    def allocate_then_timeout(params, timeout):
        sandbox = Sandbox("allocated-but-unreturned", labels=params.labels)
        controller.sdk.sandboxes[sandbox.id] = sandbox
        raise DaytonaError("Create response timed out")

    controller.sdk.create = allocate_then_timeout
    with pytest.raises(DaytonaError):
        controller.tick()
    assert controller.sdk.deleted == ["allocated-but-unreturned"]
    assert job(repo, queued).attempt == 1
    assert runtime(repo).dispatch_token is None


def test_reconciliation_outage_retries_on_next_run_iteration(controller):
    class TwoIterations:
        calls = 0

        def is_set(self):
            return self.calls >= 2

        def wait(self, seconds):
            self.calls += 1

    controller.exit = TwoIterations()
    controller.reconcile = Mock(side_effect=[DaytonaError("Temporary outage"), None])
    controller.tick = Mock()
    controller.run()
    assert controller.reconcile.call_count == 2
    controller.tick.assert_called_once()
    assert controller.last_error is None


def test_duplicate_controller_deletes_only_itself(controller, config, repo):
    config.sandbox_id = "duplicate"
    owner, duplicate, worker = (
        Sandbox("owner", "controller"),
        Sandbox("duplicate", "controller"),
        Sandbox("active-worker"),
    )
    controller.sdk.sandboxes = {s.id: s for s in [owner, duplicate, worker]}
    with repo.tx(lock=True) as session:
        session.get(Runtime, 1).controller_sandbox_id = owner.id
    controller.run()
    assert controller.sdk.deleted == [duplicate.id]
    assert runtime(repo).controller_sandbox_id == owner.id
    assert controller.exit.is_set()


def test_controller_recreation_retires_stopped_previous_owner(controller, config, repo):
    config.sandbox_id = "replacement"
    old = Sandbox("old", "controller", "stopped")
    controller.sdk.sandboxes[old.id] = old
    with repo.tx(lock=True) as session:
        session.get(Runtime, 1).controller_sandbox_id = old.id
    controller.elect()
    assert controller.sdk.deleted == [old.id]
    assert runtime(repo).controller_sandbox_id == "replacement"


def test_running_command_without_claim_has_bounded_restarts(controller, repo, queued):
    for _ in range(repo.settings.max_attempts):
        controller.tick()
        controller.dispatch.sandbox.process.get_session_command.return_value.exit_code = 1
        controller.tick()
    assert job(repo, queued).state == "failed"
    assert job(repo, queued).attempt == repo.settings.max_attempts
    controller.tick()
    assert len(controller.sdk.created) == repo.settings.max_attempts


def test_crashed_cleanup_keeps_controller_alive_until_orphan_is_deleted(controller, config, repo):
    config.sandbox_id = "controller"
    own, orphan = Sandbox("controller", "controller"), Sandbox("orphan")
    controller.sdk.sandboxes = {own.id: own, orphan.id: orphan}
    with repo.tx(lock=True) as session:
        item = session.get(Runtime, 1)
        item.worker_sandbox_id = orphan.id
        item.last_activity = time.time() - 601
    controller.tick()
    own.stop.assert_not_called()
    assert controller.sdk.deleted == [orphan.id]
    controller.tick()
    own.stop.assert_called_once()


def test_controller_election_outage_retries_before_worker_reconciliation(controller):
    class TwoIterations:
        calls = 0

        def is_set(self):
            return self.calls >= 2

        def wait(self, seconds):
            self.calls += 1

    controller.exit = TwoIterations()
    controller.elect = Mock(
        side_effect=[DomainError("controller_unverified", "Temporary outage", 503), None]
    )
    controller.reconcile = Mock()
    controller.tick = Mock()
    controller.run()
    assert controller.elect.call_count == 2
    controller.reconcile.assert_called_once()
    controller.tick.assert_called_once()
    assert controller.last_error is None


def test_explicit_stop_stops_scheduler_before_deleting_workers_and_relists():
    worker, own = Sandbox("worker"), Sandbox("controller", "controller")
    sdk = Daytona(worker, own)
    events = []

    def stop_controller(**kwargs):
        events.append("controller stopped")
        own.state = "stopped"
        sdk.sandboxes["racing-worker"] = Sandbox("racing-worker")

    own.stop.side_effect = stop_controller
    sdk.on_delete = lambda sandbox: events.append("deleted " + sandbox.id)
    assert cloud.stop(sdk=sdk) == {"stopped": 3}
    assert events == ["controller stopped", "deleted worker", "deleted racing-worker"]
    assert set(sdk.sandboxes) == {"controller"}


def test_transitioning_controller_is_deleted_during_explicit_stop():
    own = Sandbox("controller", "controller", "starting")
    sdk = Daytona(own)
    assert cloud.stop(sdk=sdk) == {"stopped": 1}
    assert sdk.deleted == [own.id]


def test_unclaimed_running_worker_bootstrap_times_out(controller, repo, queued):
    controller.tick()
    controller.dispatch.started_at = time.time() - 241
    controller.tick()
    assert controller.dispatch is None
    assert controller.sdk.deleted == ["worker-1"]
    assert job(repo, queued).attempt == 1


def test_active_worker_is_not_retired_by_unclaimed_bootstrap_timeout(controller, repo, queued):
    controller.tick()
    claimed = repo.claim()
    controller.dispatch.started_at = time.time() - 241
    assert repo.heartbeat(queued, claimed.attempt_token, "generating")
    controller.tick()
    assert controller.dispatch is not None
    assert not controller.sdk.deleted


def test_worker_thread_count_matches_allocated_cpu(controller, config, queued):
    config.worker_cpu = 2
    controller.sdk.snapshot.get.return_value.cpu = 2
    controller.sdk.on_create = lambda sandbox: setattr(sandbox, "cpu", 2)
    controller.tick()
    assert controller.sdk.created[0].env_vars["VOICE_CPU_THREADS"] == "2"


def test_controller_create_timeout_cleans_only_its_own_allocated_resource():
    sdk = Daytona()

    def allocate_then_timeout(params, timeout):
        sdk.sandboxes["allocated"] = Sandbox("allocated", labels=params.labels)
        sdk.sandboxes["other-launch"] = Sandbox("other-launch", "controller")
        raise DaytonaError("Response lost")

    sdk.create = allocate_then_timeout
    with pytest.raises(DaytonaError, match="Response lost"):
        cloud.launch("controller-snapshot", {}, sdk=sdk)
    assert sdk.deleted == ["allocated"]
    assert "other-launch" in sdk.sandboxes


def test_controller_launch_cleanup_failure_is_reported():
    sdk = Daytona()
    sdk.on_create = lambda sandbox: setattr(
        sandbox.process.execute_session_command, "side_effect", DaytonaError("Launch failed")
    )
    sdk.delete_error = DaytonaError("Deletion failed")
    with pytest.raises(DomainError, match="cleanup is uncertain"):
        cloud.launch("controller-snapshot", {}, sdk=sdk)
    assert "worker-1" in sdk.sandboxes


def test_launcher_provider_client_reads_dotenv_without_hosted_validation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ["VOICE_DAYTONA_API_KEY", "DAYTONA_API_KEY", "VOICE_DAYTONA_API_URL"]:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "VOICE_MODE=hosted\nVOICE_DAYTONA_API_KEY=test-dotenv-key\n"
        "VOICE_DAYTONA_API_URL=https://provider.example/api\n"
    )
    factory = Mock()
    monkeypatch.setattr(cloud, "Daytona", factory)
    assert cloud.client() is factory.return_value
    config = factory.call_args.args[0]
    assert config.api_key == "test-dotenv-key"
    assert config.api_url == "https://provider.example/api"
    assert "VOICE_DAYTONA_API_KEY" not in cloud.os.environ


def test_launcher_process_environment_overrides_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("VOICE_DAYTONA_API_KEY=test-dotenv-key\n")
    monkeypatch.setenv("VOICE_DAYTONA_API_KEY", "test-environment-key")
    factory = Mock()
    monkeypatch.setattr(cloud, "Daytona", factory)
    cloud.client()
    assert factory.call_args.args[0].api_key == "test-environment-key"


def test_launcher_provider_alias_is_available_in_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VOICE_DAYTONA_API_KEY", raising=False)
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    (tmp_path / ".env").write_text("DAYTONA_API_KEY=test-provider-key\n")
    factory = Mock()
    monkeypatch.setattr(cloud, "Daytona", factory)
    cloud.client()
    assert factory.call_args.args[0].api_key == "test-provider-key"
