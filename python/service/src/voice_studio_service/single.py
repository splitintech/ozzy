"""Root lifecycle supervisor; application and PostgreSQL run as separate OS users."""

import json
import os
import secrets
import shutil
import signal
import socket
import socketserver
import subprocess
import threading
import time
from pathlib import Path

import httpx
from voice_studio_core.checkpoints import CheckpointStore
from voice_studio_core.contracts import DomainError
from voice_studio_core.volume_storage import VolumeStorage

CONTROL = Path("/run/voice-control/control.sock")
BOOTSTRAP = Path("/dev/shm/voice-bootstrap/config.json")
PGDATA = Path("/var/lib/voice/postgresql")


def command(args, user=None, **kwargs):
    return subprocess.run(
        (["runuser", "-u", user, "--"] if user else []) + args,
        check=True,
        capture_output=True,
        timeout=180,
        **kwargs,
    )


def sql(statement, database="postgres"):
    return command(
        ["psql", "-h", "/run/postgresql", "-d", database, "-v", "ON_ERROR_STOP=1", "-c", statement],
        "postgres",
    )


def control(action):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(300)
        connection.connect(str(CONTROL))
        connection.sendall((action + "\n").encode())
        with connection.makefile("rb") as stream:
            response = json.loads(stream.readline(1_000_000))
        if response.get("error"):
            raise DomainError("control_failed", response["error"], 503)
        return response


class Supervisor:
    def __init__(self, payload):
        self.payload = payload
        self.manifest = payload["deployment"]
        self.token = secrets.token_urlsafe(40)
        self.process = None
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.booted = time.time()
        self.state = "restoring"
        self.ready_once = False

    def app_control(self, action):
        response = httpx.post(
            "http://127.0.0.1:8765/_runtime/" + action,
            headers={"X-Voice-Control": self.token},
            timeout=180,
            trust_env=False,
        )
        response.raise_for_status()
        return response.json()

    def database(self):
        for path, owner in (
            (PGDATA, "postgres"),
            (Path("/run/postgresql"), "postgres"),
            (Path("/tmp/voice-data"), "daytona"),
            (Path("/mnt/voice-objects"), "daytona"),
            (Path("/mnt/voice-checkpoints"), "daytona"),
        ):
            path.mkdir(parents=True, exist_ok=True)
            try:
                shutil.chown(path, owner, owner)
            except OSError:
                if not str(path).startswith("/mnt/voice-"):
                    raise
                # FUSE may not implement chown. Actual unprivileged access is checked below.
        command(
            [
                "/opt/voice/.venv/bin/python",
                "-c",
                "from voice_studio_core.volume_storage import VolumeStorage; from pathlib import Path; import tempfile,uuid; "
                "f=tempfile.NamedTemporaryFile(); f.write(b'voice-storage-probe'); f.flush(); "
                "key='health/'+uuid.uuid4().hex; "
                "stores=[VolumeStorage('/mnt/voice-objects'),VolumeStorage('/mnt/voice-checkpoints')]; "
                "[(s.put(key,f.name),s.read(key)==b'voice-storage-probe',s.delete(key)) for s in stores]",
            ],
            "daytona",
        )
        PGDATA.chmod(0o700)
        if not (PGDATA / "PG_VERSION").exists():
            command(
                ["initdb", "-D", str(PGDATA), "--auth-local=peer", "--auth-host=reject"], "postgres"
            )
        (PGDATA / "pg_hba.conf").write_text(
            "local all postgres peer\nlocal voice_studio voice_migrator peer map=voice\nlocal voice_studio voice_app peer map=voice\nlocal voice_studio voice_backup peer map=voice\n"
        )
        (PGDATA / "pg_ident.conf").write_text(
            "voice voice_migrator voice_migrator\nvoice daytona voice_app\nvoice daytona voice_backup\nvoice voice_backup voice_backup\n"
        )
        (PGDATA / "postgresql.auto.conf").write_text(
            "listen_addresses = ''\nunix_socket_directories = '/run/postgresql'\nmax_connections = 24\nshared_buffers = '128MB'\nfsync = on\nfull_page_writes = on\nsynchronous_commit = on\n"
        )
        command(
            ["pg_ctl", "-D", str(PGDATA), "-l", str(PGDATA / "server.log"), "-w", "start"],
            "postgres",
        )
        sql(
            "DO $$ BEGIN CREATE ROLE voice_migrator LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$; DO $$ BEGIN CREATE ROLE voice_app LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$; DO $$ BEGIN CREATE ROLE voice_backup LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
        )
        store = CheckpointStore(
            "/mnt/voice-checkpoints", self.payload["backup_key"], self.manifest["deployment"]
        )
        latest = store.load()
        if self.manifest["initialized"] and latest is None:
            raise DomainError(
                "recovery_degraded", "An initialized deployment has no verified checkpoint", 503
            )
        if latest:
            manifest, dump = latest
            if manifest["schema"] > self.manifest["database_schema"]:
                raise DomainError(
                    "schema_incompatible", "Checkpoint requires a newer application", 409
                )
            storage = VolumeStorage("/mnt/voice-objects")
            for item in manifest["objects"]:
                receipt = storage.verify(item["key"])
                if any(receipt[k] != item[k] for k in ("checksum", "size")):
                    raise DomainError(
                        "recovery_degraded", "A checkpoint object failed verification", 503
                    )
            # There is one fenced runtime. Always discard only unacknowledged local changes.
            sql("DROP DATABASE IF EXISTS voice_studio WITH (FORCE)")
        else:
            dump = None
        exists = sql("SELECT 1 FROM pg_database WHERE datname='voice_studio'").stdout
        if b"(1 row)" not in exists:
            sql("CREATE DATABASE voice_studio OWNER voice_migrator")
        sql(
            "REVOKE ALL ON DATABASE voice_studio FROM PUBLIC; GRANT CONNECT ON DATABASE voice_studio TO voice_migrator, voice_app, voice_backup"
        )
        sql(
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC; ALTER SCHEMA public OWNER TO voice_migrator",
            "voice_studio",
        )
        if dump:
            command(
                [
                    "pg_restore",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    "-h",
                    "/run/postgresql",
                    "-d",
                    "voice_studio",
                ],
                "voice_migrator",
                input=dump,
            )
        if dump:
            restored_revision = command(
                [
                    "psql",
                    "-h",
                    "/run/postgresql",
                    "-d",
                    "voice_studio",
                    "-At",
                    "-c",
                    "SELECT revision FROM durable_state WHERE id=1",
                ],
                "postgres",
            ).stdout.strip()
            if restored_revision != str(manifest["revision"]).encode():
                raise DomainError(
                    "recovery_degraded",
                    "Restored database revision differs from the verified checkpoint",
                    503,
                )
        command(
            [
                "/opt/voice/.venv/bin/python",
                "-c",
                "from sqlalchemy import create_engine; from voice_studio_core.migrations import migrate; migrate(create_engine('postgresql+psycopg://voice_migrator@/voice_studio?host=/run/postgresql'))",
            ],
            "voice_migrator",
        )
        sql(
            "GRANT USAGE ON SCHEMA public TO voice_app, voice_backup; GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO voice_app; GRANT SELECT ON ALL TABLES IN SCHEMA public TO voice_backup; GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO voice_app; REVOKE ALL ON voice_schema FROM voice_app; GRANT SELECT ON voice_schema TO voice_app",
            "voice_studio",
        )
        # Any restored leases belong to the previous process incarnation.
        sql(
            "UPDATE jobs SET lease_until=0, attempt_token=NULL WHERE state IN ('starting','loading','generating','processing'); UPDATE runtime SET active_job=NULL",
            "voice_studio",
        )

    def start(self):
        self.database()
        environment = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("VOICE_", "DAYTONA_", "AWS_", "S3_"))
        }
        environment.update(
            {
                "VOICE_PROFILE": "hosted_daytona_single",
                "VOICE_BIND_HOST": "0.0.0.0",
                "VOICE_DATABASE_URL": "postgresql+psycopg://voice_app@/voice_studio?host=/run/postgresql",
                "VOICE_DATA_DIR": "/tmp/voice-data",
                "VOICE_MODEL_DIR": "/opt/voice/models",
                "VOICE_STATIC_DIR": "/opt/voice/studio",
                "VOICE_MIGRATE_ON_START": "false",
                "VOICE_OWNER_PASSWORD_HASH": self.payload["owner_password_hash"],
                "VOICE_SESSION_SECRET": self.payload["session_secret"],
                "VOICE_BACKUP_KEY": self.payload["backup_key"],
                "VOICE_DEPLOYMENT_ID": self.manifest["deployment"],
                "VOICE_CONTROL_TOKEN": self.token,
            }
        )
        self.process = subprocess.Popen(
            ["runuser", "-u", "daytona", "--", "/opt/voice/.venv/bin/voice", "serve"],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.state = "starting"

    def dispatch(self, action):
        with self.lock:
            if action not in {"status", "backup", "stop"}:
                return {"error": "Unsupported runtime action"}
            if self.done.is_set():
                return {"stopped": True, "ready": False}
            result = self.app_control("drain" if action == "stop" else action)
            if action == "stop":
                if not result.get("drained"):
                    return result
                self.process.terminate()
                self.process.wait(timeout=30)
                command(["pg_ctl", "-D", str(PGDATA), "-m", "fast", "-w", "stop"], "postgres")
                self.state = "stopped"
                self.done.set()
                return {
                    "stopped": True,
                    "ready": False,
                    "recovery_required": result.get("recovery_required", False),
                }
            if result.get("ready") and not self.ready_once:
                self.booted, self.ready_once = time.time(), True
            result.update(
                {
                    "deployment": self.manifest["deployment"],
                    "state": "ready",
                    "disk": dict(
                        zip(("total", "used", "free"), shutil.disk_usage("/var/lib/voice"))
                    ),
                }
            )
            return result

    def monitor(self):
        from daytona import Daytona, DaytonaConfig

        sdk = Daytona(
            DaytonaConfig(
                api_key=self.payload["api_key"], target=self.manifest["region"], otel_enabled=False
            )
        )
        sandbox = sdk.get(self.payload["sandbox_id"])
        while not self.done.wait(20):
            try:
                status = self.dispatch("status")
                if status.get("recovery_required"):
                    self.state = "recovery_required"
                    continue
                idle = time.time() - max(status["last_activity"], self.booted)
                if idle >= self.manifest["idle_seconds"] and status["active_jobs"] == 0:
                    if self.dispatch("stop").get("stopped"):
                        sandbox.stop()
                        return
                else:
                    sandbox.refresh_activity()  # Provider deadline is a backstop, never the primary shutdown.
            except Exception:
                # Failed checkpoints prevent graceful stop. Provider backstop may still interrupt;
                # only previously verified acknowledgments are recoverable.
                self.state = "storage_unavailable"


def supervise():
    import fcntl

    CONTROL.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (CONTROL.parent / "writer.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # Local disk, never FUSE.
        payload = json.loads(BOOTSTRAP.read_text())
        BOOTSTRAP.unlink()
        supervisor = Supervisor(payload)
        supervisor.start()
        CONTROL.unlink(missing_ok=True)

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    result = supervisor.dispatch(self.rfile.readline(64).decode().strip())
                except Exception:
                    result = {"error": "Runtime not ready; inspect redacted deployment status"}
                self.wfile.write(json.dumps(result).encode() + b"\n")

        with socketserver.ThreadingUnixStreamServer(str(CONTROL), Handler) as server:
            CONTROL.chmod(0o600)
            threading.Thread(target=supervisor.monitor, daemon=True).start()
            signal.signal(signal.SIGTERM, lambda *_: supervisor.dispatch("stop"))
            server.serve_forever()
