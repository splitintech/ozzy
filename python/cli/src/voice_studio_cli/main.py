import json
import os
import secrets
import signal
import sys
import time
from pathlib import Path
from typing import Annotated

import httpx
import typer
from pydantic import ValidationError
from voice_studio_core.config import Settings
from voice_studio_core.contracts import DomainError

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Private, portable reference-conditioned speech.",
)
cloud = typer.Typer(no_args_is_help=True)
models = typer.Typer(no_args_is_help=True)
app.add_typer(cloud, name="cloud")
app.add_typer(models, name="models")


def emit(value):
    typer.echo(json.dumps(value, indent=2))


def settings():
    return Settings().prepare()


def serve_app(config):
    import uvicorn
    from voice_studio_service.api import create_app

    uvicorn.run(
        create_app(config),
        host=config.bind_host,
        port=config.port,
        access_log=False,
        proxy_headers=config.mode == "hosted",
        forwarded_allow_ips="127.0.0.1",
    )


@app.command()
def local(port: int = 8765, device: str = "cpu", dev: bool = False):
    """Start a loopback service. The per-launch access key is never stored in browser storage."""
    config = settings()
    if config.mode != "local":
        raise typer.BadParameter("voice local requires VOICE_MODE=local")
    config.port, config.device = port, device
    if device not in {"cpu", "mps", "cuda"}:
        raise typer.BadParameter("Device must be cpu, mps, or cuda")
    if dev:
        config.allowed_origins += ["http://localhost:5173", "http://127.0.0.1:5173"]
    if not config.local_token.get_secret_value():
        from pydantic import SecretStr

        config.local_token = SecretStr(secrets.token_urlsafe(32))
        typer.echo(f"Local access key (this launch only): {config.local_token.get_secret_value()}")
    typer.echo(
        f"Local key lifetime: at most {config.local_token_ttl_seconds // 60} minutes from first use "
        "by this workspace. Restart with a fresh key after expiry; reusing a configured key "
        "does not renew it. The sign-in screen shows its end time."
    )
    typer.echo(f"Voice Studio: http://127.0.0.1:{port}")
    serve_app(config)


@app.command(hidden=True)
def serve():
    serve_app(settings())


@app.command(hidden=True)
def worker():
    from voice_studio_core.database import Repository
    from voice_studio_core.storage import storage_for
    from voice_studio_service.worker import Worker

    config = settings()
    repo = Repository(config)
    repo.initialize()
    runner = Worker(repo, storage_for(config))
    signal.signal(signal.SIGTERM, lambda *_: runner.stop())
    try:
        runner.run(exit_when_empty=True)
    finally:
        repo.dispose()


@cloud.command("start")
def cloud_start(
    snapshot: str = "",
    deployment: str = "voice-studio",
    secret_names: str = "",
    json_output: Annotated[bool, typer.Option("--json")] = False,
    open_browser: Annotated[bool, typer.Option("--open")] = False,
):
    """Start/reconnect to the private controller. A returned signed URL expires in one hour."""
    from voice_studio_service.deployment import DeploymentManager

    manager = DeploymentManager()
    if manager.exists(deployment) and not snapshot:
        result = manager.start(deployment)
        if open_browser:
            import webbrowser

            webbrowser.open(result["url"])
        emit(result)
        return
    from voice_studio_service.cloud import launch

    config = Settings()
    mapping = json.loads(secret_names) if secret_names else config.daytona_secret_names
    if not isinstance(mapping, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()
    ):
        raise typer.BadParameter(
            "Secret mapping must contain environment names and Daytona secret names"
        )
    result = launch(snapshot or config.daytona_controller_snapshot, mapping, deployment)
    emit(result)


@cloud.command("status")
def cloud_status(deployment: str = "voice-studio"):
    from voice_studio_service.cloud import status
    from voice_studio_service.deployment import DeploymentManager

    manager = DeploymentManager()
    emit(
        manager.control(deployment, "status") if manager.exists(deployment) else status(deployment)
    )


@cloud.command("stop")
def cloud_stop(deployment: str = "voice-studio"):
    from voice_studio_service.cloud import stop
    from voice_studio_service.deployment import DeploymentManager

    manager = DeploymentManager()
    emit(manager.control(deployment, "stop") if manager.exists(deployment) else stop(deployment))


def protected_config(path):
    import stat

    if path is None:
        return {}
    if path.is_symlink() or (
        os.name != "nt" and (path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
    ):
        raise DomainError(
            "config_permissions", "Credential config must be a private file (chmod 600)", 400
        )
    return json.loads(path.read_text())


@cloud.command("setup")
def cloud_setup(
    deployment: str = "my-studio",
    source: Path | None = None,
    config: Path | None = None,
    region: str | None = None,
    dry_run: bool = False,
    no_open: bool = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    credentials_stdin: Annotated[bool, typer.Option("--credentials-stdin", hidden=True)] = False,
):
    """Build remotely, provision persistent storage, and open a private hosted studio."""
    from voice_studio_service.deployment import DeploymentManager

    values = json.load(sys.stdin) if credentials_stdin else protected_config(config)
    manager = DeploymentManager(
        progress=lambda phase: typer.echo(json.dumps({"phase": phase}), err=True)
    )
    if (
        not dry_run
        and not manager.vault.get(deployment)
        and sys.stdin.isatty()
        and not credentials_stdin
    ):
        values.setdefault("api_key", typer.prompt("Daytona API key", hide_input=True))
        values.setdefault(
            "owner_password",
            typer.prompt("Studio owner password", hide_input=True, confirmation_prompt=True),
        )
    result = manager.setup(
        deployment,
        source=source,
        region=region or values.get("region"),
        api_key=values.get("api_key"),
        owner_password=values.get("owner_password"),
        dry_run=dry_run,
    )
    if not dry_run and not no_open:
        import webbrowser

        webbrowser.open(result["url"])
    emit(result)


@cloud.command("destroy")
def cloud_destroy(deployment: str = "my-studio", purge_data: bool = False, confirm: str = ""):
    """Remove compute; volumes are retained unless explicitly purged with a matching name."""
    from voice_studio_service.deployment import DeploymentManager

    emit(DeploymentManager().destroy(deployment, purge_data=purge_data, confirm=confirm))


@cloud.command("update")
def cloud_update(source: Path, deployment: str = "my-studio"):
    from voice_studio_service.deployment import DeploymentManager

    emit(DeploymentManager().update(deployment, source=source))


@cloud.command("rollback")
def cloud_rollback(deployment: str = "my-studio"):
    """Return to the recorded compatible image using the newest checkpoint, never older data."""
    from voice_studio_service.deployment import DeploymentManager

    emit(DeploymentManager().update(deployment, rollback=True))


@cloud.command("deployments")
def cloud_deployments():
    from voice_studio_service.deployment import DeploymentManager

    emit({"deployments": [p.stem for p in DeploymentManager().root.glob("*.json")]})


@cloud.command("backup")
def cloud_backup(deployment: str = "my-studio"):
    from voice_studio_service.deployment import DeploymentManager

    emit(DeploymentManager().control(deployment, "backup"))


@cloud.command("restore")
def cloud_restore(deployment: str = "my-studio", no_open: bool = False):
    """Recover only after confirmed sandbox loss, using the newest complete checkpoint."""
    from voice_studio_service.deployment import DeploymentManager

    manager = DeploymentManager()
    manifest = manager.load(deployment)
    if manager.discover(manager.connect(deployment), manifest) is not None:
        raise DomainError(
            "sandbox_exists",
            "Use start for an existing sandbox; restore requires confirmed loss",
            409,
        )
    result = manager.start(deployment)
    if not no_open:
        import webbrowser

        webbrowser.open(result["url"])
    emit(result)


@cloud.command("recovery-export")
def recovery_export(output: Path, deployment: str = "my-studio", config: Path | None = None):
    from voice_studio_service.deployment import DeploymentManager

    phrase = protected_config(config).get("recovery_passphrase")
    if not phrase and sys.stdin.isatty():
        phrase = typer.prompt(
            "Separate recovery passphrase", hide_input=True, confirmation_prompt=True
        )
    if not phrase:
        raise DomainError("passphrase_required", "Provide a protected recovery passphrase", 400)
    emit(DeploymentManager().recovery_export(deployment, phrase, output))


@cloud.command("recovery-import")
def recovery_import(bundle: Path, config: Path | None = None):
    from voice_studio_service.deployment import DeploymentManager

    phrase = protected_config(config).get("recovery_passphrase")
    if not phrase and sys.stdin.isatty():
        phrase = typer.prompt("Recovery passphrase", hide_input=True)
    if not phrase:
        raise DomainError("passphrase_required", "Provide a protected recovery passphrase", 400)
    emit(DeploymentManager().recovery_import(bundle, phrase))


single = typer.Typer(hidden=True)
app.add_typer(single, name="single")


@single.command("supervise")
def single_supervise():
    from voice_studio_service.single import supervise

    supervise()


@single.command("control")
def single_control(action: str):
    from voice_studio_service.single import control

    emit(control(action))


@single.command("validate")
def single_validate():
    """Candidate validation uses no mounted production data."""
    import subprocess

    from voice_studio_engine.models import verify_model

    version = subprocess.run(
        ["postgres", "--version"], check=True, capture_output=True, text=True
    ).stdout
    if " 17." not in version or not verify_model(Path("/opt/voice/models"), "nano", deep=True):
        raise DomainError("candidate_invalid", "PostgreSQL or model validation failed", 503)
    import base64

    from voice_studio_service.auth import passwords
    from voice_studio_service.single import Supervisor

    # The candidate has no production mounts or credentials. Exercise migrations,
    # restricted roles, API startup and a real checkpoint before replacing a writer.
    supervisor = Supervisor(
        {
            "owner_password_hash": passwords.hash(secrets.token_urlsafe(24)),
            "session_secret": secrets.token_urlsafe(48),
            "backup_key": base64.b64encode(os.urandom(32)).decode(),
            "deployment": {
                "deployment": "candidate-validation",
                "initialized": False,
                "database_schema": 2,
            },
        }
    )
    supervisor.start()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            status = supervisor.dispatch("status")
            if status.get("ready"):
                if not supervisor.dispatch("stop").get("stopped"):
                    raise DomainError("candidate_invalid", "Candidate failed orderly shutdown", 503)
                emit({"validated": True, "schema": 2, "model": "nano", "postgres": 17})
                return
        except httpx.HTTPError:
            pass
        if supervisor.process.poll() is not None:
            break
        time.sleep(1)
    raise DomainError("candidate_invalid", "Candidate API or checkpoint readiness failed", 503)


@cloud.command("snapshot")
def cloud_snapshot(name: str, image: str, cpu: int = 4, memory: int = 8, disk: int = 10):
    """Create a CPU snapshot from an immutable, published image digest."""
    from daytona import CreateSnapshotParams, Resources
    from voice_studio_service.cloud import client

    if "@sha256:" not in image or len(image.rsplit("@sha256:", 1)[1]) != 64:
        raise typer.BadParameter("Use an immutable image@sha256:digest")
    if cpu not in {1, 2, 4, 8}:
        raise typer.BadParameter(
            "Choose 1, 2, 4, or 8 CPUs; account quota must permit the allocation"
        )
    result = client().snapshot.create(
        CreateSnapshotParams(
            name=name, image=image, resources=Resources(cpu=cpu, memory=memory, disk=disk)
        ),
        timeout=600,
    )
    emit({"name": result.name, "state": str(result.state)})


@cloud.command("configure-storage")
def configure_storage():
    """Apply expiry to the dedicated private bucket, preserving unrelated lifecycle rules."""
    from voice_studio_core.storage import S3Storage

    config = settings()
    if config.mode != "hosted":
        raise typer.BadParameter(
            "Set VOICE_MODE=hosted and configure your dedicated private bucket"
        )
    S3Storage(config).configure_retention(config.retention_days)
    emit({"retention_days": config.retention_days, "prefix": "expiring/"})


@models.command("install")
def model_install(model: Annotated[str, typer.Argument()] = "nano"):
    from voice_studio_engine.models import install_model

    config = settings()
    path = install_model(
        config.model_dir,
        model,
        lambda index, total, name: typer.echo(
            json.dumps({"index": index, "total": total, "file": name}), err=True
        ),
    )
    emit({"installed": str(path)})


@models.command("status")
def model_status():
    from voice_studio_engine.models import model_lock, verify_model

    config = settings()
    emit({name: verify_model(config.model_dir, name, deep=True) for name in model_lock()["models"]})


@app.command()
def generate(
    text: Annotated[str, typer.Option(help="The complete English script")],
    voice_id: Annotated[str, typer.Option(help="The approved immutable voice version ID")],
    output: Path = Path("message.mp3"),
    model: str = "nano",
    url: str = "http://127.0.0.1:8765",
    seed: int = 42,
    timeout: int = 1200,
    idempotency_key: str = "",
):
    """Submit and download a message. Set VOICE_API_KEY; start the session beforehand."""
    token = os.getenv("VOICE_API_KEY", os.getenv("VOICE_LOCAL_TOKEN", ""))
    if not token:
        raise typer.BadParameter("Set VOICE_API_KEY or VOICE_LOCAL_TOKEN")
    if not (url.startswith("https://") or url.startswith("http://127.0.0.1:")):
        raise typer.BadParameter("Use HTTPS for hosted access")
    # Preserve signed preview query parameters for every request.
    base = httpx.URL(url)

    def endpoint(path):
        return base.copy_with(path=path)

    with httpx.Client(
        headers={"Authorization": f"Bearer {token}"}, timeout=60, follow_redirects=False
    ) as client:
        response = client.post(
            endpoint("/v1/generations"),
            headers={"Idempotency-Key": idempotency_key or secrets.token_urlsafe(24)},
            json={"voice_id": voice_id, "text": text, "options": {"model": model, "seed": seed}},
        )
        response.raise_for_status()
        job = response.json()
        deadline = time.monotonic() + timeout
        while job["state"] not in {"completed", "failed", "cancelled"}:
            if time.monotonic() > deadline:
                raise DomainError(
                    "client_timeout",
                    f"Polling timed out; job {job['id']} may still be running",
                    504,
                )
            time.sleep(2)
            response = client.get(endpoint(f"/v1/generations/{job['id']}"))
            response.raise_for_status()
            job = response.json()
        if job["state"] != "completed":
            emit({"id": job["id"], "state": job["state"], "error": job.get("error")})
            raise typer.Exit(1)
        kind = "wav" if output.suffix.lower() == ".wav" else "mp3"
        artifact = next(a for a in job["artifacts"] if a["kind"] == kind)
        response = client.get(endpoint(artifact["url"]))
        response.raise_for_status()
        import hashlib

        if hashlib.sha256(response.content).hexdigest() != artifact["checksum"]:
            raise DomainError(
                "artifact_integrity", "The downloaded artifact failed verification", 502
            )
        output.write_bytes(response.content)
        emit({"id": job["id"], "output": str(output.resolve()), "timings": job["timings"]})


@app.command("password-hash")
def password_hash():
    """Create an Argon2 owner hash using hidden input; the password is not stored."""
    from voice_studio_service.auth import passwords

    value = typer.prompt("Owner password", hide_input=True, confirmation_prompt=True)
    if len(value) < 12:
        raise typer.BadParameter("Use at least twelve characters")
    typer.echo(passwords.hash(value))


@app.command()
def doctor(check_acceleration: bool = False):
    import importlib.util
    import platform
    import shutil

    config = settings()
    available_devices = ["cpu"]
    if check_acceleration and importlib.util.find_spec("torch") is not None:
        import torch

        if torch.backends.mps.is_available():
            available_devices.append("mps")
        if torch.cuda.is_available():
            available_devices.append("cuda")
    emit(
        {
            "available_devices": available_devices,
            "acceleration_checked": check_acceleration,
            "platform": platform.platform(),
            "python": platform.python_version(),
            "ffmpeg": bool(shutil.which("ffmpeg") or os.getenv("VOICE_FFMPEG_DIR")),
            "inference_dependencies_installed": importlib.util.find_spec("torch") is not None,
            "data_dir": str(config.data_dir),
            "mode": config.mode,
            "qualification": "unqualified until benchmark and owner review are completed",
        }
    )


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--engine-runner":
        from voice_studio_engine.runner import main as run_engine

        run_engine()
        return
    try:
        app()
    except ValidationError:
        emit(
            {
                "code": "configuration_invalid",
                "message": "Configuration is incomplete or invalid. Check the example configuration; secret values are never printed.",
            }
        )
        raise SystemExit(1) from None
    except (json.JSONDecodeError, httpx.HTTPError):
        emit(
            {
                "code": "request_failed",
                "message": "The request or configuration was invalid. Check authentication and session status.",
            }
        )
        raise SystemExit(1) from None
    except DomainError as exc:
        emit({"code": exc.code, "message": exc.message})
        raise SystemExit(1) from None
    except Exception:
        emit(
            {
                "code": "operation_failed",
                "message": "Operation failed. Check protected credentials, account quota and deployment status; retry to reconcile.",
            }
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
