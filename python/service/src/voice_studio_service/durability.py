"""Serialize owner-visible publication while allowing inference/lease heartbeats."""

import hashlib
import json
import secrets
from contextlib import contextmanager
from email.parser import BytesParser
from email.policy import default

from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from voice_studio_core.contracts import DomainError

from .auth import authenticate


@contextmanager
def publication(repo):
    coordinator = repo.durability
    if coordinator is None:
        yield
        return
    with coordinator.lock:
        coordinator.ensure_available()
        yield
        coordinator.checkpoint()


def request_fingerprint(method, path, body, content_type):
    if content_type.startswith("multipart/form-data"):
        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
        )
        values = [
            (
                part.get_param("name", header="content-disposition"),
                part.get_filename(),
                hashlib.sha256(part.get_payload(decode=True)).hexdigest(),
            )
            for part in message.iter_parts()
        ]
        body = json.dumps(values, sort_keys=True).encode()
    return hashlib.sha256(method.encode() + path.encode() + body).hexdigest()


async def durable_request(request, call_next):
    coordinator = request.app.state.repo.durability
    if (
        coordinator is None
        or request.url.path in {"/health", "/v1/session", "/v1/durability"}
        or not request.url.path.startswith(("/v1/", "/ready"))
    ):
        return await call_next(request)
    await run_in_threadpool(coordinator.lock.acquire)
    changed = recorded = False
    identifier = None
    try:
        try:
            await run_in_threadpool(coordinator.ensure_available)
            import time

            from sqlalchemy import func, select
            from voice_studio_core.database import Job, Voice

            repo = request.app.state.repo
            if repo.settings.profile == "hosted_daytona_single":
                with repo.tx() as session:
                    due = session.scalar(
                        select(func.count(Job.id)).where(
                            Job.deleted.is_(False), Job.expires_at <= time.time()
                        )
                    )
                    due += session.scalar(
                        select(func.count(Voice.id)).where(
                            Voice.deleted.is_(False), Voice.created_at <= time.time() - 30 * 86400
                        )
                    )
                if due:
                    from .jobs import maintenance

                    await run_in_threadpool(maintenance, repo, request.app.state.storage)
                    await run_in_threadpool(coordinator.checkpoint)
                    await run_in_threadpool(maintenance, repo, request.app.state.storage, True)
            mutating = (
                request.method in {"POST", "PUT", "PATCH", "DELETE"}
                and request.url.path != "/v1/activity"
            )
            if not mutating:
                return await call_next(request)
            # Authenticate before replay too: revoked keys must not retrieve old responses.
            principal = None
            if request.url.path != "/v1/auth/login":
                principal = authenticate(request)
            else:
                origin = request.headers.get("origin")
                own = request.app.state.settings.public_origin or f"https://{request.url.netloc}"
                if (origin and origin != own) or request.headers.get(
                    "sec-fetch-site"
                ) == "cross-site":
                    raise DomainError("origin_forbidden", "Use the application's sign-in form", 403)
            key = request.headers.get("Idempotency-Key") or secrets.token_urlsafe(24)
            if len(key) > 128 or not key.isascii():
                raise DomainError("invalid_idempotency_key", "Invalid operation key", 400)
            identity = request.headers.get("authorization", "") or request.cookies.get(
                "voice_session", ""
            )
            # Owner session renewal must not create a second mutation after a lost response.
            # API-key callers retain separate namespaces and are authenticated before replay.
            if principal in {"local", "session"}:
                identity = "owner"
            identifier = hashlib.sha256((identity + ":" + key).encode()).hexdigest()
            body = await request.body()
            fingerprint = request_fingerprint(
                request.method, request.url.path, body, request.headers.get("content-type", "")
            )
            previous = await run_in_threadpool(coordinator.receipt, identifier, fingerprint)
            if previous:
                data = json.loads(bytes.fromhex(previous["body"]))
                with repo.tx() as session:
                    entity = None
                    if request.url.path.startswith("/v1/generations") and data.get("id"):
                        entity = session.get(Job, data["id"])
                    elif request.url.path.startswith("/v1/voices") and data.get("id"):
                        entity = session.get(Voice, data["id"])
                    if entity is not None and (
                        entity.deleted
                        or (
                            getattr(entity, "expires_at", None) and entity.expires_at <= time.time()
                        )
                    ):
                        raise DomainError(
                            "operation_expired",
                            "The content for this operation was deleted or expired",
                            410,
                        )
                return Response(
                    bytes.fromhex(previous["body"]),
                    status_code=previous["status"],
                    headers=previous["headers"],
                )
            changed = True
            response = await call_next(request)
            if response.status_code >= 400:
                changed = False
                return response
            # Mutating endpoints produce bounded JSON responses, never audio streams.
            chunks = [chunk async for chunk in response.body_iterator]
            saved = {
                "body": b"".join(chunks).hex(),
                "status": response.status_code,
                "headers": dict(response.headers),
            }
            saved["headers"]["x-voice-operation-id"] = identifier
            await run_in_threadpool(repo.activity)
            await run_in_threadpool(coordinator.record, identifier, fingerprint, saved)
            recorded = True
            await run_in_threadpool(coordinator.checkpoint)
            from .jobs import maintenance

            await run_in_threadpool(
                maintenance, request.app.state.repo, request.app.state.storage, True
            )
            return Response(
                bytes.fromhex(saved["body"]), status_code=saved["status"], headers=saved["headers"]
            )
        except DomainError as exc:
            return JSONResponse(
                {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "operation_id": identifier,
                },
                status_code=exc.status,
            )
        except Exception:
            coordinator.blocked = True
            return JSONResponse(
                {
                    "code": "storage_unavailable",
                    "message": "Saving is pending. Retry with the same operation key.",
                    "retryable": True,
                    "operation_id": identifier,
                },
                status_code=503,
            )
    finally:
        if changed and not recorded:
            coordinator.blocked = coordinator.recover_required = True
        coordinator.lock.release()
