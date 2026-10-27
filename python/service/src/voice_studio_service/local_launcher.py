"""Loopback owner launcher. Provider credentials stay in the server's OS keychain."""

import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from fastapi import Request
from pydantic import BaseModel, Field
from voice_studio_core.contracts import DomainError

from .auth import authenticate_local_access
from .deployment import DeploymentManager


class LaunchRequest(BaseModel):
    deployment: str = Field(default="my-studio", pattern=r"^[a-z][a-z0-9-]{0,47}$")
    action: str = Field(pattern=r"^(setup|start|status|stop|backup)$")
    source: str | None = None
    region: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    owner_password: str | None = Field(default=None, repr=False)


def install_routes(app):
    if app.state.settings.mode != "local":
        return
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hosted-launcher")
    lock = threading.Lock()
    operations = {}
    manager = DeploymentManager(root=app.state.settings.launcher_dir)
    app.state.launcher_manager = manager

    def authorize(request):
        settings = app.state.settings
        origin = request.headers.get("origin")
        allowed = {
            f"http://127.0.0.1:{settings.port}",
            f"http://localhost:{settings.port}",
            *settings.allowed_origins,
        }
        host = urlsplit(str(request.url)).hostname
        # TestClient's virtual peer is supported only by explicitly overriding this dependency in tests.
        if (
            host not in {"localhost", "127.0.0.1", "::1"}
            or not request.client
            or request.client.host not in {"127.0.0.1", "::1"}
            or origin not in allowed
            or urlsplit(origin).hostname not in {"127.0.0.1", "localhost", "::1"}
        ):
            raise DomainError("launcher_forbidden", "Use the authenticated localhost launcher", 403)
        authorization = request.headers.get("authorization", "")
        bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not authenticate_local_access(request, bearer):
            raise DomainError("unauthorized", "The local launch access key is required", 401)

    @app.get("/launcher/deployments")
    def deployments(request: Request):
        authorize(request)
        return {"deployments": [p.stem for p in manager.root.glob("*.json")]}

    @app.post("/launcher/operations")
    def launch(body: LaunchRequest, request: Request):
        authorize(request)
        import uuid

        with lock:
            if any(o["state"] == "running" for o in operations.values()):
                raise DomainError("launcher_busy", "A hosted operation is already running", 409)
            identifier = uuid.uuid4().hex
            operations.clear()  # Keep signed URLs and progress bounded to one local session.
            operation = {"id": identifier, "state": "running", "phase": "connecting"}
            operations[identifier] = operation

        def run():
            try:
                manager.progress = lambda phase: operation.update(phase=phase)
                if body.action == "setup":
                    result = manager.setup(
                        body.deployment,
                        source=body.source,
                        region=body.region,
                        api_key=body.api_key,
                        owner_password=body.owner_password,
                    )
                elif body.action == "start":
                    result = manager.start(body.deployment)
                else:
                    result = manager.control(body.deployment, body.action)
                operation.update(state="completed", phase="ready", result=result)
            except DomainError as exc:
                operation.update(state="failed", error=exc.message, code=exc.code)
            except Exception:
                operation.update(
                    state="failed",
                    error="Hosted operation failed. Check credentials, quota and deployment status; retry to reconcile.",
                    code="launch_failed",
                )
            finally:
                body.api_key = body.owner_password = None

        pool.submit(run)
        return {"id": identifier, "state": "running"}

    @app.get("/launcher/operations/{identifier}")
    def progress(identifier: str, request: Request):
        authorize(request)
        with lock:
            if identifier not in operations:
                raise DomainError("not_found", "Operation not found", 404)
            return dict(operations[identifier])
