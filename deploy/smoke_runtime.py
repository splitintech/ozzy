"""Exercise a frozen runtime with shell PATH cleared; optional authorized inference fixture."""

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def exercise_workflow(base, token, reference, output):
    """Use the packaged API/worker/FFmpeg, including authorization and durable exports."""
    def request(path, method="GET", body=None, content_type="application/json"):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {token}"}
        if body is not None:
            headers["Content-Type"] = content_type
        with urllib.request.urlopen(urllib.request.Request(
            base + path, data=body, headers=headers, method=method
        ), timeout=90) as reply:
            return reply.read()

    def json_request(*args, **kwargs):
        return json.loads(request(*args, **kwargs))

    try:
        urllib.request.urlopen(base + "/v1/voices", timeout=5)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
    else:
        raise RuntimeError("Packaged API accepted an unauthenticated read")
    boundary = "voice-smoke-" + secrets.token_hex(16)
    parts = []
    for name, value in {"name": "Runtime smoke reference", "authorized": "true", "start": "0"}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts += [f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="reference.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(),
              reference.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()]
    voice = json_request("/v1/voices", "POST", b"".join(parts), f"multipart/form-data; boundary={boundary}")

    def generate(purpose):
        job = json_request("/v1/generations", "POST", {
            "voice_id": voice["id"], "purpose": purpose, "text": "Please leave a message.",
            "options": {"model": "nano", "seed": 42},
        })
        deadline = time.monotonic() + 900
        while job["state"] not in {"completed", "failed", "cancelled"}:
            if time.monotonic() >= deadline:
                raise RuntimeError("Frozen API generation timed out")
            time.sleep(0.5)
            job = json_request(f"/v1/generations/{job['id']}")
        if job["state"] != "completed":
            raise RuntimeError(f"Frozen API generation failed: {job.get('error')}")
        return job

    audition = generate("audition")
    json_request(f"/v1/voices/{voice['id']}/activate", "POST", {
        "audition_job_id": audition["id"], "approved": True,
    })
    job = generate("message")
    exports = {}
    for artifact in job["artifacts"]:
        data = request(artifact["url"])
        if hashlib.sha256(data).hexdigest() != artifact["checksum"]:
            raise RuntimeError("Packaged export checksum mismatch")
        suffix = {"mp3": ".mp3", "manifest": ".json"}.get(artifact["kind"], ".wav")
        (output / (artifact["kind"] + suffix)).write_bytes(data)
        exports[artifact["kind"]] = {"bytes": len(data), "sha256": artifact["checksum"]}
    if not {"wav", "mp3", "telephone", "master", "manifest"} <= exports.keys():
        raise RuntimeError("Packaged workflow did not produce all export profiles")
    json_request(f"/v1/voices/{voice['id']}", "DELETE")
    try:
        request(job["artifacts"][0]["url"])
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    else:
        raise RuntimeError("Deleted packaged export remained accessible")
    return {"import_audition_activate_generate_export_delete": True, "exports": exports,
            "timings": job["timings"], "owner_quality_qualified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--authorized", action="store_true")
    parser.add_argument("--cpu-threads", type=int, choices=(1, 2, 4, 8), default=4)
    args = parser.parse_args()
    if args.reference and (not args.authorized or not args.models):
        parser.error(
            "Inference requires --models and explicit --authorized reference acknowledgment"
        )
    binary = args.executable.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("VOICE_", "DAYTONA_", "PYTHON")):
            env.pop(key)
    token = secrets.token_urlsafe(32)
    env.update(
        {
            "PATH": "",
            "VOICE_MODE": "local",
            "VOICE_DEVICE": "cpu",
            "VOICE_CPU_THREADS": str(args.cpu_threads),
            "VOICE_DATA_DIR": str(output / "data"),
            "VOICE_LOCAL_TOKEN": token,
            "HF_HOME": str(output / "hf"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "NUMBA_CACHE_DIR": str(output / "numba"),
            "XDG_CACHE_HOME": str(output / "cache"),
        }
    )
    if args.models:
        env["VOICE_MODEL_DIR"] = str(args.models.resolve())
    report = {
        "development_only": True,
        "owner_quality_qualified": False,
        "path_cleared": True,
        "executable": str(binary),
        "cpu_threads": args.cpu_threads,
    }
    for name, command in (("doctor", ["doctor"]), ("imports", ["--runtime-self-test"])):
        result = subprocess.run(
            [str(binary), *command],
            env=env,
            cwd=output,
            capture_output=True,
            text=True,
            timeout=240,
        )
        (output / f"{name}.stdout.log").write_text(result.stdout)
        (output / f"{name}.stderr.log").write_text(result.stderr)
        if result.returncode:
            raise RuntimeError(f"Packaged {name} failed; inspect captured logs")
        report[name] = json.JSONDecoder().raw_decode(result.stdout[result.stdout.index("{") :])[0]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (output / "service.log").open("w") as logs:
        child = subprocess.Popen(
            [str(binary), "local", "--port", str(port)],
            env=env,
            cwd=output,
            stdout=logs,
            stderr=logs,
        )
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError("Frozen local service exited before readiness")
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=2
                    ) as reply:
                        report["health"] = json.load(reply)
                    break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(0.5)
            else:
                raise RuntimeError("Frozen local service did not become healthy")
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/capabilities",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(request, timeout=10) as reply:
                report["capabilities"] = json.load(reply)
            if args.reference:
                report["workflow"] = exercise_workflow(
                    f"http://127.0.0.1:{port}", token, args.reference.resolve(), output
                )
        finally:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    if args.reference:
        reference = args.reference.resolve()
        payload = {
            "model_dir": str(args.models.resolve()),
            "reference": str(reference),
            "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
            "voice_id": "synthetic-frozen-smoke",
            "device": "cpu",
            "cpu_threads": args.cpu_threads,
            "text": "Please leave a message.",
            "options": {"model": "nano", "seed": 42},
            "output": str(output / "engine"),
        }
        result = subprocess.run(
            [str(binary), "--engine-runner", "--offline-smoke"],
            input=json.dumps(payload) + "\n",
            env=env,
            cwd=output,
            capture_output=True,
            text=True,
            timeout=900,
        )
        (output / "engine.stdout.log").write_text(result.stdout)
        (output / "engine.stderr.log").write_text(result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        if result.returncode or not events or events[-1].get("event") != "completed":
            raise RuntimeError("Frozen inference did not complete; inspect captured logs")
        report["inference"] = events[-1]
        report["inference_network_blocked"] = True
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": True,
                "report": str(output / "report.json"),
                "owner_quality_qualified": False,
            }
        )
    )


if __name__ == "__main__":
    main()
