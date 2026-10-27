import hashlib
import hmac
import secrets
import time

from fastapi import Request
from pwdlib import PasswordHash
from sqlalchemy import select, update
from voice_studio_core.contracts import DomainError
from voice_studio_core.database import Credential, LoginThrottle

passwords = PasswordHash.recommended()


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def initialize_local_access(app):
    """Persist only a digest and fixed deadline; a restart cannot renew the same key."""
    settings, repo = app.state.settings, app.state.repo
    app.state.local_access_expires_at = None
    app.state.local_access_deadline = None
    if settings.mode != "local" or not settings.local_token.get_secret_value():
        return
    fingerprint = digest(settings.local_token.get_secret_value())
    now = time.time()
    with repo.tx(lock=True) as session:
        # Retired per-launch keys must not authenticate through the API-key fallback.
        session.execute(
            update(Credential)
            .where(Credential.kind == "local_access", Credential.digest != fingerprint)
            .values(revoked=True)
        )
        credential = session.scalar(select(Credential).where(Credential.digest == fingerprint))
        if credential is None:
            credential = Credential(
                digest=fingerprint,
                kind="local_access",
                name="Local workspace access",
                created_at=now,
                expires_at=now + settings.local_token_ttl_seconds,
            )
            session.add(credential)
        elif credential.kind != "local_access":
            raise ValueError("Use a new random local access key, not an existing API credential")
        else:
            # Tightening the configured lifetime can shorten, never extend, a key's life.
            credential.expires_at = min(
                credential.expires_at or credential.created_at,
                credential.created_at + settings.local_token_ttl_seconds,
            )
        expires_at = credential.expires_at
        if credential.revoked:
            expires_at = min(expires_at, now)
    app.state.local_access_expires_at = expires_at
    app.state.local_access_deadline = time.monotonic() + max(0, expires_at - now)


def authenticate_local_access(request, bearer):
    settings = request.app.state.settings
    expected = settings.local_token.get_secret_value()
    if (
        settings.mode != "local"
        or not expected
        or not bearer
        or not hmac.compare_digest(expected, bearer)
    ):
        return False
    deadline = getattr(request.app.state, "local_access_deadline", None)
    with request.app.state.repo.tx() as session:
        credential = session.scalar(select(Credential).where(Credential.digest == digest(bearer)))
        if (
            deadline is None
            or time.monotonic() >= deadline
            or not credential
            or credential.kind != "local_access"
            or credential.revoked
            or credential.expires_at is None
            or time.time() >= credential.expires_at
        ):
            raise DomainError(
                "local_access_expired",
                "Your local access key has expired. Reopen the local workspace in the desktop app, "
                "or restart voice local for a new key. If VOICE_LOCAL_TOKEN is configured, replace "
                "or unset it first. Your saved voices and takes are unchanged.",
                401,
            )
    return True


def authenticate(request: Request):
    settings = request.app.state.settings
    origin = request.headers.get("origin")
    own_origin = (
        settings.public_origin
        or f"{'https' if settings.mode == 'hosted' else request.url.scheme}://{request.url.netloc}"
    )
    allowed = {own_origin, *settings.allowed_origins}
    if origin and origin not in allowed:
        raise DomainError("origin_forbidden", "This origin is not allowed", 403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise DomainError("origin_forbidden", "Cross-site requests are not allowed", 403)
    authorization = request.headers.get("authorization", "")
    bearer = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    if authenticate_local_access(request, bearer):
        return "local"
    token = (
        bearer if settings.mode == "local" else (bearer or request.cookies.get("voice_session", ""))
    )
    if not token:
        raise DomainError("unauthorized", "Sign in to Voice Studio", 401)
    with request.app.state.repo.tx() as s:
        credential = s.scalar(select(Credential).where(Credential.digest == digest(token)))
        if (
            not credential
            or credential.kind not in {"api", "session"}
            or credential.revoked
            or (credential.expires_at and credential.expires_at <= time.time())
        ):
            raise DomainError("unauthorized", "Your session or API key has expired", 401)
        if not bearer and request.method not in {"GET", "HEAD", "OPTIONS"}:
            if origin != own_origin:
                raise DomainError("csrf_forbidden", "A same-origin request is required", 403)
        return credential.kind


def login(request, password):
    settings, repo = request.app.state.settings, request.app.state.repo
    if settings.mode != "hosted":
        raise DomainError("local_session", "Local mode uses a per-launch access key", 400)
    # Avoid trusting caller-supplied proxy headers for rate-limit identities.
    identity = request.client.host if request.client else "unknown"
    key = hmac.new(
        settings.session_secret.get_secret_value().encode(), identity.encode(), hashlib.sha256
    ).hexdigest()
    with repo.tx(lock=True) as s:
        record = s.get(LoginThrottle, key)
        if record and time.time() - record.window_start < 300 and record.count >= 5:
            raise DomainError(
                "login_throttled", "Too many attempts. Try again in five minutes.", 429
            )
        if not record:
            record = LoginThrottle(key=key, count=0, window_start=time.time())
            s.add(record)
        if time.time() - record.window_start >= 300:
            record.count, record.window_start = 0, time.time()
        record.count += 1
    try:
        valid = passwords.verify(password, settings.owner_password_hash.get_secret_value())
    except Exception:
        valid = False
    if not valid:
        raise DomainError("login_failed", "The password is incorrect", 401)
    token = secrets.token_urlsafe(32)
    with repo.tx(lock=True) as s:
        s.add(
            Credential(
                digest=digest(token),
                kind="session",
                name="Owner session",
                expires_at=time.time() + 8 * 3600,
            )
        )
        s.get(LoginThrottle, key).count = 0
    return token
