"""Private supervisor control. Tokens never enter the hosted client or inference child."""

import hmac
import time

from fastapi import Request
from sqlalchemy import func, select
from voice_studio_core.contracts import ACTIVE_STATES, DomainError
from voice_studio_core.database import Job, Runtime


def install_runtime_routes(app):
    if app.state.settings.profile != "hosted_daytona_single":
        return
    app.state.draining = False
    started = time.monotonic()
    charge_id = "single-" + str(time.time_ns())

    @app.post("/_runtime/{action}", include_in_schema=False)
    def runtime_control(action: str, request: Request):
        cfg, repo = app.state.settings, app.state.repo
        expected = cfg.control_token.get_secret_value()
        if (
            not expected
            or not hmac.compare_digest(request.headers.get("X-Voice-Control", ""), expected)
            or not request.client
            or request.client.host != "127.0.0.1"
        ):
            raise DomainError("not_found", "Not found", 404)
        if action not in {"status", "backup", "drain"}:
            raise DomainError("not_found", "Not found", 404)
        coordinator = repo.durability
        with coordinator.lock:
            seconds = time.monotonic() - started
            repo.charge(charge_id, "single", seconds, cfg.single_hourly_usd)
            with repo.tx() as session:
                active = session.scalar(
                    select(func.count(Job.id)).where(Job.state.in_(["queued", *ACTIVE_STATES]))
                )
                last_activity = session.get(Runtime, 1).last_activity
            references = [item["key"] for item in (coordinator.last or {}).get("objects", [])]
            result = {
                "ready": not coordinator.blocked and not app.state.draining,
                "database_ready": True,
                "recovery_required": coordinator.recover_required,
                "model_ready": bool(app.state.worker and app.state.worker.engine.ready),
                "active_jobs": active,
                "last_activity": last_activity,
                "checkpoint": (
                    {k: coordinator.last[k] for k in ("revision", "created_at", "schema")}
                    if coordinator.last
                    else None
                ),
                "durability": "storage_unavailable" if coordinator.blocked else "saved",
                "cost_basis": "application estimate, not provider billing",
                "cleanup_backlog": app.state.storage.cleanup_backlog(references),
            }
            if action == "status":
                return result
            if action == "drain" and coordinator.recover_required:
                # Preserve the last acknowledged checkpoint, never publish an uncertain mutation.
                if coordinator.store.load() is None:
                    raise DomainError(
                        "recovery_degraded", "No verified recovery checkpoint is available", 503
                    )
                app.state.draining = True
                if app.state.worker:
                    app.state.worker.stop()
                return {**result, "drained": True, "recovery_required": True}
            if action == "drain" and active:
                return {
                    **result,
                    "drained": False,
                    "reason": "Outstanding jobs must finish or be cancelled",
                }
            if action == "drain":
                app.state.draining = True
            try:
                from .jobs import maintenance

                maintenance(repo, app.state.storage)
                checkpoint = coordinator.checkpoint()
                maintenance(repo, app.state.storage, True)
                app.state.storage.cleanup_orphans([item["key"] for item in checkpoint["objects"]])
                if action == "drain" and app.state.worker:
                    app.state.worker.stop()
                return {
                    **result,
                    "checkpoint": {k: checkpoint[k] for k in ("revision", "created_at", "schema")},
                    "drained": action == "drain",
                }
            except BaseException:
                app.state.draining = False
                raise
