"""Disposable combined-image acceptance. Uses test data and local Docker volumes, never Daytona."""

import argparse
import array
import base64
import json
import math
import os
import subprocess
import time
import uuid
import wave
from pathlib import Path

import httpx
from pwdlib import PasswordHash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="voice-studio:single")
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--inference", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("artifacts/single-container.json"))
    args = parser.parse_args()
    docker = os.getenv("VOICE_TEST_DOCKER", "docker")
    name = "voice-single-test-" + uuid.uuid4().hex[:8]
    objects, checkpoints = name + "-objects", name + "-checkpoints"
    report = {
        "image": args.image,
        "provider": "local Docker, not Daytona",
        "checks": [],
        "inference": False,
    }

    def run(*cmd, input=None, check=True):
        result = subprocess.run(
            [docker, *cmd], input=input, check=False, capture_output=True, timeout=300
        )
        if check and result.returncode:
            raise RuntimeError(
                "Container command failed: " + result.stderr.decode(errors="replace")[-3000:]
            )
        return result

    def inside(*cmd, **kw):
        return run("exec", name, *cmd, **kw)

    payload = {
        "api_key": "fixture-unused",
        "owner_password_hash": PasswordHash.recommended().hash("test-owner-password"),
        "session_secret": "s" * 48,
        "backup_key": base64.b64encode(b"b" * 32).decode(),
        "sandbox_id": name,
        "deployment": {
            "deployment": "container-test",
            "initialized": False,
            "database_schema": 2,
            "region": None,
            "idle_seconds": 600,
        },
    }

    report["image_id"] = json.loads(run("image", "inspect", args.image).stdout)[0]["Id"]
    report["cpu_limit"] = 4
    report["memory_limit_gib"] = 8
    report["startup_seconds"] = []

    def launch(initialized):
        began = time.monotonic()
        print("Restoring compute" if initialized else "Initializing compute", flush=True)
        run(
            "run",
            "--platform",
            "linux/amd64",
            "-d",
            "--name",
            name,
            "--cpus",
            "4",
            "--memory",
            "8g",
            "-p",
            f"127.0.0.1:{args.port}:8765",
            "-v",
            objects + ":/mnt/voice-objects",
            "-v",
            checkpoints + ":/mnt/voice-checkpoints",
            args.image,
        )
        payload["deployment"]["initialized"] = initialized
        code = "import os,sys; from pathlib import Path; p=Path('/dev/shm/voice-bootstrap'); p.mkdir(mode=0o700); f=p/'config.json'; f.write_bytes(sys.stdin.buffer.read()); f.chmod(0o600)"
        run(
            "exec",
            "-i",
            name,
            "/opt/voice/.venv/bin/python",
            "-c",
            code,
            input=json.dumps(payload).encode(),
        )
        # Only provider calls are disabled. Database, API, worker and storage are real.
        run(
            "exec",
            "-d",
            name,
            "/bin/sh",
            "-c",
            "/opt/voice/.venv/bin/python -c 'import subprocess,sys; original=subprocess.Popen; subprocess.Popen=lambda *a,**k: original(*a,**{**k,\"stderr\":sys.stderr}); from voice_studio_service.single import Supervisor,supervise; Supervisor.monitor=lambda self: None; supervise()' > /tmp/supervisor.log 2>&1",
        )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status = inside(
                "/opt/voice/.venv/bin/voice", "single", "control", "status", check=False
            )
            if status.returncode == 0 and json.loads(status.stdout).get("ready"):
                report["startup_seconds"].append(round(time.monotonic() - began, 2))
                report["runtime_disk"] = json.loads(status.stdout).get("disk")
                print("API and checkpoint ready", flush=True)
                return
            time.sleep(2)
        log = inside("cat", "/tmp/supervisor.log", check=False).stdout.decode()
        raise RuntimeError("Container startup failed: " + log[-4000:])

    client = httpx.Client(
        base_url=f"http://127.0.0.1:{args.port}",
        timeout=180,
        trust_env=False,
        headers={"Origin": f"https://127.0.0.1:{args.port}"},
    )

    def call(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path}: {response.status_code} {response.text[:500]}")
        return response

    def login():
        response = call("POST", "/v1/auth/login", json={"password": "test-owner-password"})
        client.headers["Authorization"] = "Bearer " + response.cookies["voice_session"]

    try:
        launch(False)
        report["checks"].append("automatic initialization and API readiness")
        assert (
            inside(
                "runuser",
                "-u",
                "daytona",
                "--",
                "psql",
                "-h",
                "/run/postgresql",
                "-U",
                "voice_app",
                "-d",
                "voice_studio",
                "-c",
                "CREATE TABLE forbidden(id int)",
                check=False,
            ).returncode
            != 0
        )
        assert (
            inside(
                "runuser",
                "-u",
                "daytona",
                "--",
                "psql",
                "-h",
                "/run/postgresql",
                "-U",
                "voice_backup",
                "-d",
                "voice_studio",
                "-c",
                "DELETE FROM voices",
                check=False,
            ).returncode
            != 0
        )
        assert (
            inside(
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-h",
                "/run/postgresql",
                "-At",
                "-c",
                "SHOW listen_addresses",
            ).stdout.strip()
            == b""
        )
        assert (
            inside("test", "-f", "/dev/shm/voice-bootstrap/config.json", check=False).returncode
            != 0
        )
        report["checks"].append(
            "restricted application and backup roles; Unix socket only; consumed bootstrap"
        )
        login()
        key = call(
            "POST",
            "/v1/keys",
            headers={"Idempotency-Key": "test-key"},
            json={"name": "Revoked on restore"},
        ).json()
        login()  # Retry the same owner action after a renewed login session.
        assert (
            call(
                "POST",
                "/v1/keys",
                headers={"Idempotency-Key": "test-key"},
                json={"name": "Revoked on restore"},
            ).json()
            == key
        )
        call("DELETE", "/v1/keys/" + key["id"], headers={"Idempotency-Key": "revoke-key"})
        import io

        recording = io.BytesIO()
        with wave.open(recording, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(
                array.array(
                    "h",
                    (int(7000 * math.sin(2 * math.pi * 180 * i / 24000)) for i in range(8 * 24000)),
                ).tobytes()
            )
        voice = call(
            "POST",
            "/v1/voices",
            headers={"Idempotency-Key": "import-test"},
            files={"file": ("fixture.wav", recording.getvalue(), "audio/wav")},
            data={
                "name": "Synthetic container fixture",
                "authorized": "true",
                "start": "0",
                "end": "8",
            },
        ).json()
        if args.inference:
            print("Generating with real CPU Chatterbox Nano", flush=True)
            inference_started = time.monotonic()
            job = call(
                "POST",
                "/v1/generations",
                headers={"Idempotency-Key": "audition-test"},
                json={
                    "voice_id": voice["id"],
                    "text": "Hello. Please leave a message.",
                    "purpose": "audition",
                    "options": {"model": "nano", "seed": 42},
                },
            ).json()
            deadline = time.monotonic() + 1200
            while (
                job["state"] not in {"completed", "failed", "cancelled"}
                and time.monotonic() < deadline
            ):
                time.sleep(3)
                job = call("GET", "/v1/generations/" + job["id"]).json()
            assert job["state"] == "completed", job
            report["generation_seconds"] = round(time.monotonic() - inference_started, 2)
            call(
                "POST",
                "/v1/voices/" + voice["id"] + "/activate",
                headers={"Idempotency-Key": "approve-test"},
                json={"audition_job_id": job["id"], "approved": True},
            )
            print("Generating an approved voice take", flush=True)
            warm_started = time.monotonic()
            job = call(
                "POST",
                "/v1/generations",
                headers={"Idempotency-Key": "approved-take"},
                json={
                    "voice_id": voice["id"],
                    "text": "Thank you for calling.",
                    "purpose": "message",
                    "options": {"model": "nano", "seed": 43},
                },
            ).json()
            deadline = time.monotonic() + 1200
            while (
                job["state"] not in {"completed", "failed", "cancelled"}
                and time.monotonic() < deadline
            ):
                time.sleep(3)
                job = call("GET", "/v1/generations/" + job["id"]).json()
            assert job["state"] == "completed", job
            report["warm_generation_seconds"] = round(time.monotonic() - warm_started, 2)
            for artifact in job["artifacts"]:
                assert call("GET", artifact["url"]).content
            report["checks"].append(
                "real Nano audition, approval, message generation and completed export download"
            )
            report["inference"] = True
        report["checks"].append(
            "checkpointed import and idempotent key creation across renewed sessions/revocation"
        )
        stopped = json.loads(
            inside("/opt/voice/.venv/bin/voice", "single", "control", "stop").stdout
        )
        assert stopped["stopped"]
        assert (
            inside(
                "runuser",
                "-u",
                "postgres",
                "--",
                "pg_isready",
                "-h",
                "/run/postgresql",
                check=False,
            ).returncode
            != 0
        )
        report["checks"].append("graceful database shutdown")
        run("rm", "-fv", name)
        launch(True)
        login()
        assert any(v["id"] == voice["id"] for v in call("GET", "/v1/voices").json())
        assert next(k for k in call("GET", "/v1/keys").json() if k["id"] == key["id"])["revoked"]
        assert (
            client.get("/v1/voices", headers={"Authorization": "Bearer " + key["key"]}).status_code
            == 401
        )
        if args.inference:
            restored = call("GET", "/v1/generations/" + job["id"]).json()
            assert restored["state"] == "completed"
            assert call("GET", restored["artifacts"][0]["url"]).content
        call("DELETE", "/v1/voices/" + voice["id"], headers={"Idempotency-Key": "delete-voice"})
        report["checks"].append("compute-container deletion recovery and credential revocation")
        inside("/opt/voice/.venv/bin/voice", "single", "control", "stop")
        run("rm", "-fv", name)
        launch(True)
        login()
        assert not call("GET", "/v1/voices").json()
        report["checks"].append("durable deletion does not resurrect after restore")
        report["passed"] = True
    finally:
        client.close()
        run("rm", "-fv", name, check=False)
        run("volume", "rm", objects, checkpoints, check=False)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
