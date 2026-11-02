#!/usr/bin/env python3
"""Embed Ozzy as the speak layer of an in-app voice agent.

Your app still transcribes and writes the reply. This client only clones,
auditions, and generates speech. It does not call OpenAI Realtime.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Resemble Labs Original defaults for TTS and voice agents.
ORIGINAL_PROFILE = {
    "model": "original",
    "seed": 42,
    "temperature": 0.8,
    "exaggeration": 0.5,
    "cfg_weight": 0.5,
}


def _origin() -> str:
    origin = os.environ.get("VOICE_ORIGIN", "http://127.0.0.1:8765").rstrip("/")
    if not origin:
        raise SystemExit("Set VOICE_ORIGIN")
    return origin


def _request(method: str, path: str, *, payload: dict | None = None, body: bytes | None = None, content_type: str | None = None) -> dict:
    key = os.environ.get("VOICE_API_KEY") or os.environ.get("VOICE_LOCAL_TOKEN") or ""
    if not key:
        raise SystemExit("Set VOICE_API_KEY or VOICE_LOCAL_TOKEN")
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Idempotency-Key": str(uuid.uuid4()),
    }
    data = body
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    elif content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(_origin() + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} failed: {exc.code} {exc.read().decode()[:400]}") from exc
    return json.loads(raw) if raw else {}


def _poll(job: dict) -> dict:
    latest = job
    for _ in range(80):
        state = str(latest.get("state") or "").lower()
        if state == "completed":
            return latest
        if state in {"failed", "cancelled", "error"}:
            raise SystemExit(f"generation {state}")
        job_id = latest.get("id")
        if not job_id:
            return latest
        time.sleep(1.5)
        latest = _request("GET", f"/v1/generations/{job_id}")
    raise SystemExit("generation timed out")


def clone(reference: Path, name: str) -> str:
    boundary = uuid.uuid4().hex
    filename = reference.name or "reference.wav"
    file_bytes = reference.read_bytes()
    chunks = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\n{name}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"authorized\"\r\n\r\ntrue\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"start\"\r\n\r\n0\r\n".encode(),
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            "Content-Type: audio/wav\r\n\r\n"
        ).encode()
        + file_bytes
        + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    created = _request(
        "POST",
        "/v1/voices",
        body=b"".join(chunks),
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    voice_id = str(created.get("id") or "")
    audition = _poll(
        _request(
            "POST",
            "/v1/generations",
            payload={"voice_id": voice_id, "text": "audition", "purpose": "audition", "options": ORIGINAL_PROFILE},
        )
    )
    _request(
        "POST",
        f"/v1/voices/{voice_id}/activate",
        payload={"audition_job_id": audition.get("id"), "approved": True},
    )
    print(voice_id)
    return voice_id


def speak(voice_id: str, text: str) -> dict:
    job = _poll(
        _request(
            "POST",
            "/v1/generations",
            payload={"voice_id": voice_id, "text": text, "purpose": "message", "options": ORIGINAL_PROFILE},
        )
    )
    artifacts = job.get("artifacts") if isinstance(job.get("artifacts"), list) else []
    url = next((item.get("url") for item in artifacts if isinstance(item, dict) and item.get("url")), job.get("url"))
    print(url or job.get("id"))
    return job


def main(argv: list[str]) -> None:
    if len(argv) >= 3 and argv[1] == "clone":
        clone(Path(argv[2]), " ".join(argv[3:]) or "In-app voice")
        return
    if len(argv) >= 4 and argv[1] == "speak":
        speak(argv[2], " ".join(argv[3:]))
        return
    raise SystemExit("usage: client.py clone REFERENCE.wav [name]\n       client.py speak VOICE_ID TEXT")


if __name__ == "__main__":
    main(sys.argv)
