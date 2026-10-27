import hashlib
import io
import json
import secrets
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    Request,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import or_, select
from starlette.background import BackgroundTask
from voice_studio_core.audio import prepare_reference, probe
from voice_studio_core.config import Settings
from voice_studio_core.contracts import (
    ActivationRequest,
    CapabilitiesResponse,
    DomainError,
    DurabilityStatus,
    GenerationRequest,
    JobResponse,
    KeyRequest,
    LoginRequest,
    OperationStatus,
    SessionResponse,
    VoiceResponse,
)
from voice_studio_core.database import Artifact, Credential, Job, Repository, Voice, uid
from voice_studio_core.storage import storage_for
from voice_studio_engine.models import install_model, model_lock, verify_model

from .auth import authenticate, digest, initialize_local_access, login
from .jobs import job_view, maintenance, submit
from .worker import Worker


def voice_view(voice):
    return {
        "id": voice.id,
        "name": voice.name,
        "created_at": voice.created_at,
        "active": voice.active,
        "authorized": voice.authorized,
        "checksum": voice.checksum,
        "details": voice.details,
    }


def create_app(settings=None, repository=None, storage=None, engine=None):
    settings = (settings or Settings()).prepare()
    repo = repository or Repository(settings)
    objects = storage or storage_for(settings)

    @asynccontextmanager
    async def lifespan(app):
        repo.initialize()
        initialize_local_access(app)
        repo.recover()
        if settings.profile == "hosted_daytona_single":
            from voice_studio_core.checkpoints import DurabilityCoordinator
            repo.durability = DurabilityCoordinator(repo, objects)
            with repo.durability.lock:
                maintenance(repo, objects)
                repo.durability.checkpoint()
                maintenance(repo, objects, True)
                objects.cleanup_orphans([item["key"] for item in repo.durability.last["objects"]])
        worker = thread = controller = None
        if settings.worker_enabled:
            if settings.mode == "local" or settings.profile == "hosted_daytona_single":
                worker = Worker(repo, objects, engine)
                thread = threading.Thread(target=worker.run, daemon=True)
                thread.start()
            else:
                from .cloud import Controller

                controller = Controller(settings, repo, objects)
                controller.start()
        app.state.worker = worker
        app.state.controller = controller
        try:
            yield
        finally:
            if worker:
                worker.stop()
                thread.join(timeout=10)
            if controller:
                controller.stop()
            if repository is None:
                repo.dispose()

    app = FastAPI(
        title="Voice Studio",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings, app.state.repo, app.state.storage = settings, repo, objects
    app.state.model_installs = {}
    app.state.install_lock = threading.Lock()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    @app.middleware("http")
    async def secure_headers(request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            if request.url.path.startswith("/v1/") and request.url.path != "/v1/auth/login":
                try:
                    authenticate(request)
                except DomainError as exc:
                    return JSONResponse(
                        {"code": exc.code, "message": exc.message}, status_code=exc.status
                    )
            maximum = (
                settings.max_upload_bytes + 65536 if request.url.path == "/v1/voices" else 32768
            )
            try:
                declared = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse(
                    {"code": "invalid_length", "message": "Invalid request size"}, status_code=400
                )
            if declared < 0 or declared > maximum:
                return JSONResponse(
                    {"code": "request_too_large", "message": "Request exceeds its size limit"},
                    status_code=413,
                )
            chunks, received = [], 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > maximum:
                    return JSONResponse(
                        {"code": "request_too_large", "message": "Request exceeds its size limit"},
                        status_code=413,
                    )
                chunks.append(chunk)
            request._body = b"".join(chunks)
        if getattr(app.state, "draining", False) and request.method in {"POST", "PUT", "PATCH", "DELETE"} and not request.url.path.startswith("/_runtime/"):
            return JSONResponse({"code": "draining", "message": "Studio is stopping; retry after starting it"}, status_code=503)
        from .durability import durable_request
        response = await durable_request(request, call_next)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, exc):
        return JSONResponse(
            {
                "code": "validation_error",
                "message": "; ".join(str(item["msg"]) for item in exc.errors()),
            },
            status_code=422,
        )

    @app.exception_handler(DomainError)
    async def domain_error(_, exc):
        return JSONResponse(
            {"code": exc.code, "message": exc.message, "retryable": exc.retryable},
            status_code=exc.status,
        )

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/v1/session", response_model=SessionResponse)
    def session_mode():
        return {
            "mode": settings.mode, "profile": settings.profile,
            "local_access_expires_at": getattr(app.state, "local_access_expires_at", None),
        }

    @app.post("/v1/auth/login")
    def owner_login(body: LoginRequest, request: Request):
        # Login CSRF is also rejected, before creating the session.
        origin = request.headers.get("origin")
        if origin and origin != (
            settings.public_origin
            or f"{'https' if settings.mode == 'hosted' else request.url.scheme}://{request.url.netloc}"
        ):
            raise DomainError("origin_forbidden", "Use the application's own sign-in form", 403)
        token = login(request, body.password)
        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            "voice_session",
            token,
            httponly=True,
            secure=True,
            samesite="strict",
            max_age=8 * 3600,
            path="/",
        )
        repo.activity()
        return response

    @app.post("/v1/auth/logout", dependencies=[Depends(authenticate)])
    def logout(request: Request):
        token = request.cookies.get("voice_session", "")
        with repo.tx(lock=True) as s:
            credential = s.scalar(select(Credential).where(Credential.digest == digest(token)))
            if credential:
                credential.revoked = True
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("voice_session", secure=True, httponly=True, samesite="strict")
        return response

    @app.get(
        "/v1/capabilities",
        response_model=CapabilitiesResponse,
        dependencies=[Depends(authenticate)],
    )
    def capabilities():
        lock = model_lock()
        return {
            "mode": settings.mode,
            "profile": settings.profile,
            "device": settings.device,
            "models": [
                {
                    "id": name,
                    "revision": spec["revision"],
                    "download_bytes": sum(f["size"] for f in spec["files"]),
                    "installed": verify_model(settings.model_dir, name, deep=False),
                    "configured_remote": settings.mode == "hosted"
                    and name in settings.hosted_models,
                    "supports_cfg": spec["supports_cfg"],
                    "supports_exaggeration": spec["supports_exaggeration"],
                    "install": app.state.model_installs.get(name),
                }
                for name, spec in lock["models"].items()
            ],
            "limits": {
                "script_characters": 2000,
                "script_words": 280,
                "message_seconds": 120,
                "queue": settings.max_queue,
            },
            "performance_qualified": False,
            "orchestration_error": getattr(
                getattr(app.state, "controller", None), "last_error", None
            ),
        }

    @app.get("/ready", dependencies=[Depends(authenticate)])
    def readiness():
        return {
            "api_ready": True,
            "models_installed": [
                n for n in model_lock()["models"] if verify_model(settings.model_dir, n, deep=False)
            ],
            "engine_loaded": bool(
                app.state.worker and getattr(app.state.worker.engine, "ready", False)
            ),
        }

    @app.get("/v1/openapi.json", dependencies=[Depends(authenticate)])
    def openapi():
        return app.openapi()

    @app.post("/v1/activity", dependencies=[Depends(authenticate)])
    def activity():
        repo.activity()
        return {"ok": True}

    @app.get("/v1/voices", response_model=list[VoiceResponse], dependencies=[Depends(authenticate)])
    def voices():
        with repo.tx() as s:
            return [
                voice_view(v)
                for v in s.scalars(
                    select(Voice)
                    .where(
                        Voice.deleted.is_(False),
                    )
                    .order_by(Voice.created_at.desc())
                )
            ]

    @app.post(
        "/v1/voices",
        response_model=VoiceResponse,
        status_code=201,
        dependencies=[Depends(authenticate)],
    )
    async def import_voice(
        file: UploadFile = File(...),
        name: str = Form(..., min_length=1, max_length=100),
        authorized: bool = Form(...),
        start: float = Form(0),
        end: float | None = Form(None),
    ):
        if not authorized:
            raise DomainError(
                "authorization_required", "Confirm authorization to use this voice", 422
            )
        name = name.strip()
        if not name:
            raise DomainError("voice_name_required", "Enter a name for this voice", 422)
        voice_id = uid()
        with tempfile.TemporaryDirectory(prefix="import-", dir=settings.data_dir) as temporary:
            source, reference = Path(temporary) / "source", Path(temporary) / "reference.wav"
            size = 0
            with source.open("wb") as stream:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise DomainError(
                            "upload_too_large", "Reference files must be under 50 MB", 413
                        )
                    stream.write(chunk)
            if size == 0:
                raise DomainError("empty_file", "Choose a reference recording", 422)
            from starlette.concurrency import run_in_threadpool

            info = await run_in_threadpool(probe, source)
            details = await run_in_threadpool(
                prepare_reference,
                source,
                reference,
                start,
                end if end is not None else min(info["duration"], start + 15),
                settings.max_reference_seconds,
            )
            original_key = f"voices/{voice_id}/original"
            reference_key = f"voices/{voice_id}/reference.wav"
            try:
                await run_in_threadpool(
                    objects.put,
                    original_key,
                    source,
                    "audio/mpeg" if info["format"] == "mp3" else "audio/wav",
                )
                receipt = await run_in_threadpool(
                    objects.put, reference_key, reference, "audio/wav"
                )
                with repo.tx(lock=True) as s:
                    voice = Voice(
                        id=voice_id,
                        name=name.strip(),
                        original_key=original_key,
                        reference_key=reference_key,
                        checksum=receipt["checksum"],
                        details=details,
                        authorized=True,
                    )
                    s.add(voice)
                    s.flush()
                repo.activity()
                return voice_view(voice)
            except BaseException:
                objects.delete_prefix(f"voices/{voice_id}")
                raise

    @app.post(
        "/v1/voices/{voice_id}/activate",
        response_model=VoiceResponse,
        dependencies=[Depends(authenticate)],
    )
    def activate(voice_id: str, body: ActivationRequest):
        with repo.tx(lock=True) as s:
            voice, audition = s.get(Voice, voice_id), s.get(Job, body.audition_job_id)
            if not voice or voice.deleted or (settings.profile == "hosted_daytona_single" and voice.created_at <= time.time() - 30 * 86400):
                raise DomainError("not_found", "Voice not found", 404)
            if (
                not audition
                or audition.voice_id != voice_id
                or audition.purpose != "audition"
                or audition.state != "completed"
                or audition.deleted
                or (audition.expires_at and audition.expires_at <= time.time())
            ):
                raise DomainError("audition_required", "Generate and review an audition first", 409)
            for other in s.scalars(select(Voice)):
                other.active = other.id == voice.id
            s.flush()
        repo.activity()
        return voice_view(voice)

    @app.delete("/v1/voices/{voice_id}", dependencies=[Depends(authenticate)])
    def delete_voice(voice_id: str):
        with repo.tx(lock=True) as s:
            voice = s.get(Voice, voice_id)
            if not voice:
                raise DomainError("not_found", "Voice not found", 404)
            voice.deleted, voice.active = True, False
            for job in s.scalars(select(Job).where(Job.voice_id == voice_id)):
                job.deleted, job.cancel_requested = True, True
                if job.state == "queued":
                    job.state = "cancelled"
        maintenance(repo, objects)
        return {"deleted": True}

    @app.get("/v1/voices/{voice_id}/export", dependencies=[Depends(authenticate)])
    def export_voice(voice_id: str):
        with repo.tx() as s:
            voice = s.get(Voice, voice_id)
            if not voice or voice.deleted or (settings.profile == "hosted_daytona_single" and voice.created_at <= time.time() - 30 * 86400):
                raise DomainError("not_found", "Voice not found", 404)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("reference.wav", objects.read(voice.reference_key))
            archive.writestr("voice.json", json.dumps(voice_view(voice), indent=2))
        return Response(
            data.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="voice-reference.zip"'},
        )

    @app.post(
        "/v1/generations",
        response_model=JobResponse,
        status_code=202,
        dependencies=[Depends(authenticate)],
    )
    def generate(body: GenerationRequest, idempotency_key: str | None = Header(None)):
        if settings.mode == "hosted" and body.options.model not in settings.hosted_models:
            raise DomainError(
                "model_unavailable", "This model is not configured in the hosted worker image", 409
            )
        job, created = submit(repo, objects, body, idempotency_key)
        return {**job_view(repo, objects, job), "created": created}

    @app.get(
        "/v1/generations", response_model=list[JobResponse], dependencies=[Depends(authenticate)]
    )
    def generations():
        with repo.tx() as s:
            jobs = list(
                s.scalars(
                    select(Job)
                    .where(
                        Job.deleted.is_(False),
                        or_(Job.expires_at.is_(None), Job.expires_at > time.time()),
                    )
                    .order_by(Job.created_at.desc())
                    .limit(100)
                )
            )
        return [job_view(repo, objects, job) for job in jobs]

    @app.get(
        "/v1/generations/{job_id}", response_model=JobResponse, dependencies=[Depends(authenticate)]
    )
    def generation(job_id: str):
        with repo.tx() as s:
            job = s.get(Job, job_id)
            if not job:
                raise DomainError("not_found", "Message not found", 404)
        return job_view(repo, objects, job)

    @app.post(
        "/v1/generations/{job_id}/cancel",
        response_model=JobResponse,
        dependencies=[Depends(authenticate)],
    )
    def cancel(job_id: str):
        return job_view(repo, objects, repo.cancel(job_id))

    @app.delete("/v1/generations/{job_id}", dependencies=[Depends(authenticate)])
    def delete_generation(job_id: str):
        with repo.tx(lock=True) as s:
            job = s.get(Job, job_id)
            if not job:
                raise DomainError("not_found", "Message not found", 404)
            job.deleted, job.cancel_requested = True, True
            if job.state == "queued":
                job.state = "cancelled"
        maintenance(repo, objects)
        return {"deleted": True}

    @app.get("/v1/artifacts/{artifact_id}", dependencies=[Depends(authenticate)])
    def artifact(artifact_id: str):
        with repo.tx() as s:
            item = s.get(Artifact, artifact_id)
            job = s.get(Job, item.job_id) if item else None
            if (
                not item
                or not job
                or job.deleted
                or job.state != "completed"
                or (job.expires_at and job.expires_at <= time.time())
            ):
                raise DomainError("not_found", "Audio not found or expired", 404)
        temporary = tempfile.NamedTemporaryFile(dir=settings.data_dir, delete=False)
        path = Path(temporary.name)
        temporary.close()
        try:
            objects.download(item.key, path)
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != item.checksum:
                raise DomainError(
                    "artifact_integrity", "Stored audio failed integrity verification", 500
                )
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        extension = {"mp3": ".mp3", "manifest": ".json"}.get(item.kind, ".wav")
        return FileResponse(
            path,
            media_type=item.mime_type,
            filename=f"voice-{job.id[:8]}-{item.kind}{extension}",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

    @app.post("/v1/models/{name}/install", status_code=202, dependencies=[Depends(authenticate)])
    def install(name: str):
        if name not in model_lock()["models"]:
            raise DomainError("not_found", "Model not found", 404)
        if settings.mode == "hosted":
            raise DomainError(
                "managed_model", "Hosted models are installed in the pinned worker image", 409
            )
        with app.state.install_lock:
            if any(v["state"] == "downloading" for v in app.state.model_installs.values()):
                raise DomainError("install_active", "Wait for the current model download", 409)
            app.state.model_installs[name] = {"state": "downloading", "file": None}

        def work():
            try:

                def progress(index, total, filename):
                    app.state.model_installs[name] = {
                        "state": "downloading",
                        "file": filename,
                        "completed_files": index,
                        "total_files": total,
                    }

                install_model(settings.model_dir, name, progress)
                app.state.model_installs[name] = {"state": "installed"}
            except DomainError as exc:
                app.state.model_installs[name] = {"state": "failed", "message": exc.message}
            except Exception:
                app.state.model_installs[name] = {
                    "state": "failed",
                    "message": "Download failed. Retry to resume verified files.",
                }

        threading.Thread(target=work, daemon=True).start()
        return {"state": "downloading"}

    @app.get("/v1/keys", dependencies=[Depends(authenticate)])
    def keys():
        with repo.tx() as s:
            return [
                {"id": c.id, "name": c.name, "created_at": c.created_at, "revoked": c.revoked}
                for c in s.scalars(select(Credential).where(Credential.kind == "api"))
            ]

    @app.post("/v1/keys", dependencies=[Depends(authenticate)])
    def create_key(body: KeyRequest, request: Request):
        if authenticate(request) == "api":
            raise DomainError("owner_required", "Use the owner session to create keys", 403)
        token = "vs_" + secrets.token_urlsafe(32)
        with repo.tx(lock=True) as s:
            item = Credential(digest=digest(token), kind="api", name=body.name)
            s.add(item)
            s.flush()
        return {"id": item.id, "key": token, "name": item.name}

    @app.delete("/v1/keys/{key_id}", dependencies=[Depends(authenticate)])
    def revoke_key(key_id: str):
        with repo.tx(lock=True) as s:
            item = s.get(Credential, key_id)
            if not item or item.kind != "api":
                raise DomainError("not_found", "Key not found", 404)
            item.revoked = True
        return {"revoked": True}

    @app.get("/v1/usage", dependencies=[Depends(authenticate)])
    def usage():
        with repo.tx() as s:
            budget = repo.budget(s)
        ratio = (budget["estimated_spend_usd"] + budget["reserved_usd"]) / budget["limit_usd"]
        return {
            **budget,
            "estimate_only": True,
            "percent_used": round(100 * ratio, 1),
            "budget_alert": "limit" if ratio >= 1 else "warning" if ratio >= 0.8 else None,
            "excludes": (["persistent volumes", "snapshot storage", "network transfer"] if settings.profile == "hosted_daytona_single" else ["database", "object storage", "network transfer"]),
        }

    @app.post("/v1/maintenance", dependencies=[Depends(authenticate)])
    def cleanup():
        maintenance(repo, objects)
        return {"ok": True}

    from .runtime_api import install_runtime_routes
    install_runtime_routes(app)
    from .local_launcher import install_routes
    install_routes(app)

    @app.get("/v1/durability", response_model=DurabilityStatus, dependencies=[Depends(authenticate)])
    def durability_status():
        coordinator = repo.durability
        return {"enabled": coordinator is not None,
                "state": "storage_unavailable" if coordinator and coordinator.blocked else "saved",
                "checkpoint": ({"revision": coordinator.last["revision"], "created_at": coordinator.last["created_at"], "schema_version": coordinator.last["schema"]} if coordinator and coordinator.last else None)}

    @app.get("/v1/operations/{identifier}", response_model=OperationStatus, dependencies=[Depends(authenticate)])
    def operation_status(identifier: str):
        from sqlalchemy import text
        if not repo.durability:
            raise DomainError("not_found", "Operation not found", 404)
        with repo.engine.connect() as connection:
            revision = connection.execute(text("SELECT revision FROM operation_receipts WHERE id=:id"), {"id": identifier}).scalar_one_or_none()
        if revision is None:
            raise DomainError("not_found", "Operation not found", 404)
        saved = repo.durability.last and repo.durability.last["revision"] >= revision and not repo.durability.blocked
        return {"operation_id": identifier, "revision": revision, "state": "saved" if saved else "saving"}

    if settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="studio")
    return app
