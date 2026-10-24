import json
import secrets
import tempfile
import time
from pathlib import Path

from sqlalchemy import func, select
from voice_studio_core.contracts import ACTIVE_STATES, DomainError
from voice_studio_core.database import Artifact, Job, Runtime, Voice, uid


def submit(repo, storage, request, idempotency_key):
    key = idempotency_key or secrets.token_urlsafe(24)
    if len(key) > 128 or not key.isascii():
        raise DomainError(
            "invalid_idempotency_key", "Use an ASCII idempotency key up to 128 characters"
        )
    fingerprint = request.fingerprint()
    with repo.tx(lock=True) as s:
        existing = s.scalar(select(Job).where(Job.idempotency_key == key))
        if existing:
            if existing.fingerprint != fingerprint:
                raise DomainError(
                    "idempotency_conflict", "This key was used for a different script", 409
                )
            if existing.deleted or (existing.expires_at and existing.expires_at <= time.time()):
                raise DomainError(
                    "idempotency_expired", "Use a new key for a deleted or expired request", 409
                )
            return existing, False
        voice = s.get(Voice, request.voice_id)
        if not voice or voice.deleted:
            raise DomainError("voice_not_found", "Import a reference voice first", 404)
        if request.purpose == "message" and not voice.active:
            raise DomainError("voice_not_approved", "Audition and approve this voice first", 409)
        if not voice.authorized:
            raise DomainError("voice_not_authorized", "This voice has not been authorized", 403)
        count = s.scalar(
            select(func.count(Job.id)).where(Job.state.in_(["queued", *ACTIVE_STATES]))
        )
        if count >= repo.settings.max_queue:
            raise DomainError(
                "queue_full", "The queue is full. Try again after a message finishes.", 429, True
            )
        reservation = 0.0
        if repo.settings.mode == "hosted":
            budget = repo.budget(s)
            reservation = repo.settings.job_reservation_usd
            if (
                budget["estimated_spend_usd"] + budget["reserved_usd"] + reservation
                > budget["limit_usd"]
            ):
                raise DomainError(
                    "budget_limit", "The configured hosted budget has been reached", 429
                )
        job_id = uid()
        script_key = f"expiring/jobs/{job_id}/script.json"
        with tempfile.TemporaryDirectory(dir=repo.settings.data_dir) as temp:
            script = Path(temp) / "script.json"
            script.write_text(json.dumps({"text": request.text}))
            storage.put(script_key, script, "application/json")
        job = Job(
            id=job_id,
            voice_id=voice.id,
            options=request.options.model_dump(),
            script_key=script_key,
            fingerprint=fingerprint,
            idempotency_key=key,
            purpose=request.purpose,
            reserved_usd=reservation,
            expires_at=(
                time.time() + repo.settings.retention_days * 86400
                if repo.settings.mode == "hosted"
                else None
            ),
        )
        s.add(job)
        s.get(Runtime, 1).last_activity = time.time()
        s.flush()
        return job, True


def maintenance(repo, storage):
    """Tombstone first. Retried physical cleanup never restores access."""
    with repo.tx(lock=True) as s:
        expired = list(
            s.scalars(
                select(Job).where(
                    Job.expires_at.is_not(None),
                    Job.expires_at <= time.time(),
                )
            )
        )
        for job in expired:
            job.deleted, job.cancel_requested = True, True
            if job.state == "queued":
                job.state = "cancelled"
        jobs = [(j.id, j.state) for j in s.scalars(select(Job).where(Job.deleted.is_(True)))]
        voices = [v.id for v in s.scalars(select(Voice).where(Voice.deleted.is_(True)))]
    for job_id, state in jobs:
        if state not in ACTIVE_STATES:
            storage.delete_prefix(f"expiring/jobs/{job_id}")
    for voice_id in voices:
        with repo.tx() as s:
            running = s.scalar(
                select(func.count(Job.id)).where(
                    Job.voice_id == voice_id,
                    Job.state.in_(ACTIVE_STATES),
                )
            )
        if not running:
            storage.delete_prefix(f"voices/{voice_id}")


def job_view(repo, storage, job, include_text=True):
    now = time.time()
    if job.deleted or (job.expires_at and job.expires_at <= now):
        raise DomainError("not_found", "Message not found or expired", 404)
    text = ""
    if include_text:
        try:
            text = json.loads(storage.read(job.script_key))["text"]
        except (FileNotFoundError, KeyError):
            text = ""
    with repo.tx() as s:
        voice = s.get(Voice, job.voice_id)
        artifacts = list(s.scalars(select(Artifact).where(Artifact.job_id == job.id)))
    return {
        "id": job.id,
        "voice_id": job.voice_id,
        "voice_name": voice.name if voice else "Deleted voice",
        "text": text,
        "state": job.state,
        "purpose": job.purpose,
        "options": job.options,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "expires_at": job.expires_at,
        "attempt": job.attempt,
        "cancel_requested": job.cancel_requested,
        "error": (
            {"code": job.error_code, "message": job.error_message, "retryable": job.retryable}
            if job.state == "failed"
            else None
        ),
        "timings": job.timings,
        "artifacts": [
            {
                "id": a.id,
                "kind": a.kind,
                "mime_type": a.mime_type,
                "size": a.size,
                "checksum": a.checksum,
                "duration": a.duration,
                "url": f"/v1/artifacts/{a.id}",
            }
            for a in artifacts
            if job.state == "completed"
        ],
    }
