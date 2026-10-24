"""Durable queue with a database-wide admission/claim lock.

SQLite uses BEGIN IMMEDIATE. PostgreSQL locks the singleton coordination row.
Inference never holds a database transaction open.
"""

import time
import uuid
from contextlib import contextmanager

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .contracts import ACTIVE_STATES, TERMINAL_STATES, DomainError


def uid():
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Voice(Base):
    __tablename__ = "voices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    original_key: Mapped[str] = mapped_column(String(300))
    reference_key: Mapped[str] = mapped_column(String(300))
    checksum: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON)
    authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    voice_id: Mapped[str] = mapped_column(ForeignKey("voices.id"), index=True)
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    purpose: Mapped[str] = mapped_column(String(20), default="message")
    options: Mapped[dict] = mapped_column(JSON)
    script_key: Mapped[str] = mapped_column(String(300))
    fingerprint: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    attempt_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(300), nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    timings: Mapped[dict] = mapped_column(JSON, default=dict)
    reserved_usd: Mapped[float] = mapped_column(Float, default=0)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    key: Mapped[str] = mapped_column(String(300))
    mime_type: Mapped[str] = mapped_column(String(80))
    checksum: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    duration: Mapped[float] = mapped_column(Float)


class Runtime(Base):
    __tablename__ = "runtime"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    active_job: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_activity: Mapped[float] = mapped_column(Float, default=time.time)
    controller_sandbox_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    worker_sandbox_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    dispatch_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dispatch_lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)


class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class LoginThrottle(Base):
    __tablename__ = "login_throttle"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[float] = mapped_column(Float, default=time.time)


class Charge(Base):
    __tablename__ = "charges"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)
    amount_usd: Mapped[float] = mapped_column(Float)
    seconds: Mapped[float] = mapped_column(Float)


class Repository:
    def __init__(self, settings):
        self.settings = settings
        kwargs = {"pool_pre_ping": True}
        if settings.database_url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine = create_engine(settings.database_url, **kwargs)
        self.sqlite = self.engine.dialect.name == "sqlite"
        if self.sqlite:

            @event.listens_for(self.engine, "connect")
            def sqlite_connect(dbapi, _):
                dbapi.isolation_level = None
                dbapi.execute("PRAGMA foreign_keys=ON")
                dbapi.execute("PRAGMA journal_mode=WAL")

            @event.listens_for(self.engine, "begin")
            def sqlite_begin(connection):
                connection.exec_driver_sql("BEGIN IMMEDIATE")

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        from .migrations import migrate

        migrate(self.engine)
        with self.sessions.begin() as s:
            if not self.sqlite:
                from sqlalchemy import text

                s.execute(text("SELECT pg_advisory_xact_lock(8913472901)"))
            if not s.get(Runtime, 1):
                s.add(Runtime(id=1))
        with self.tx() as s:
            if s.get(Runtime, 1).schema_version != 1:
                raise RuntimeError("Unsupported schema version; run the matching release migration")

    @contextmanager
    def tx(self, lock=False):
        with self.sessions.begin() as session:
            if lock:
                session.execute(
                    select(Runtime).where(Runtime.id == 1).with_for_update()
                ).scalar_one()
            yield session

    def activity(self):
        with self.tx(lock=True) as s:
            s.get(Runtime, 1).last_activity = time.time()

    def recover(self):
        now = time.time()
        with self.tx(lock=True) as s:
            for job in s.scalars(select(Job).where(Job.state.in_(ACTIVE_STATES))):
                if job.lease_until and job.lease_until > now:
                    continue
                if job.cancel_requested or job.deleted:
                    job.state = "cancelled"
                elif job.attempt < self.settings.max_attempts:
                    job.state = "queued"
                else:
                    job.state = "failed"
                    job.error_code = "worker_lost"
                    job.error_message = "The worker stopped before completing this message."
                    job.retryable = True
                job.attempt_token = None
                job.lease_until = None
                job.updated_at = now
            runtime = s.get(Runtime, 1)
            if runtime.active_job:
                active = s.get(Job, runtime.active_job)
                if not active or active.state not in ACTIVE_STATES:
                    runtime.active_job = None

    def claim(self):
        self.recover()
        with self.tx(lock=True) as s:
            runtime = s.get(Runtime, 1)
            if runtime.active_job:
                return None
            job = s.scalar(
                select(Job)
                .where(
                    Job.state == "queued",
                    Job.deleted.is_(False),
                    Job.cancel_requested.is_(False),
                )
                .order_by(Job.created_at)
                .limit(1)
            )
            if not job:
                return None
            if job.expires_at and job.expires_at <= time.time():
                job.state = "failed"
                job.error_code = "expired"
                job.error_message = "This request expired before it could run."
                return None
            job.state = "starting"
            job.attempt += 1
            job.attempt_token = uid()
            job.lease_until = time.time() + self.settings.lease_seconds
            job.updated_at = time.time()
            runtime.active_job = job.id
            s.flush()
            return job

    def heartbeat(self, job_id, attempt_token, state=None):
        with self.tx(lock=True) as s:
            job = s.get(Job, job_id)
            if (
                not job
                or job.attempt_token != attempt_token
                or job.state not in ACTIVE_STATES
                or job.deleted
                or job.cancel_requested
                or not job.lease_until
                or job.lease_until < time.time()
            ):
                return False
            job.lease_until = time.time() + self.settings.lease_seconds
            job.updated_at = time.time()
            if state in ACTIVE_STATES:
                order = {name: index for index, name in enumerate(ACTIVE_STATES)}
                if order[state] >= order[job.state]:
                    job.state = state
            return True

    def complete(self, job_id, token, artifacts, timings):
        with self.tx(lock=True) as s:
            job = s.get(Job, job_id)
            if (
                not job
                or job.attempt_token != token
                or job.cancel_requested
                or job.deleted
                or job.state not in ACTIVE_STATES
                or (job.expires_at and job.expires_at <= time.time())
                or not job.lease_until
                or job.lease_until < time.time()
            ):
                return False
            for item in artifacts:
                s.add(Artifact(job_id=job.id, **item))
            job.state, job.timings = "completed", timings
            job.updated_at, job.lease_until = time.time(), None
            s.get(Runtime, 1).active_job = None
            return True

    def fail(self, job_id, token, code, message, retryable=False):
        with self.tx(lock=True) as s:
            job = s.get(Job, job_id)
            if not job or job.attempt_token != token or job.state not in ACTIVE_STATES:
                return
            if job.cancel_requested or job.deleted:
                job.state = "cancelled"
            elif retryable and job.attempt < self.settings.max_attempts:
                job.state = "queued"
            else:
                job.state = "failed"
            job.error_code, job.error_message, job.retryable = code, message[:300], retryable
            job.updated_at, job.lease_until = time.time(), None
            job.attempt_token = None
            s.get(Runtime, 1).active_job = None

    def cancel(self, job_id):
        with self.tx(lock=True) as s:
            job = s.get(Job, job_id)
            if not job or job.deleted:
                raise DomainError("not_found", "Message not found", 404)
            if job.state not in TERMINAL_STATES:
                job.cancel_requested = True
                if job.state == "queued":
                    job.state = "cancelled"
            return job

    def charge(self, charge_id, kind, seconds, hourly_usd):
        with self.tx(lock=True) as s:
            existing = s.get(Charge, charge_id)
            amount = max(0, seconds) * hourly_usd / 3600
            if existing:
                existing.amount_usd, existing.seconds = amount, seconds
            else:
                s.add(Charge(id=charge_id, kind=kind, amount_usd=amount, seconds=seconds))

    def budget(self, s):
        from datetime import datetime, timezone

        start = (
            datetime.now(timezone.utc)
            .replace(
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            .timestamp()
        )
        spent = s.scalar(
            select(func.coalesce(func.sum(Charge.amount_usd), 0)).where(
                Charge.created_at >= start,
            )
        )
        reserved = s.scalar(
            select(func.coalesce(func.sum(Job.reserved_usd), 0)).where(
                Job.state.in_(["queued", *ACTIVE_STATES]),
            )
        )
        return {
            "estimated_spend_usd": spent,
            "reserved_usd": reserved,
            "limit_usd": self.settings.monthly_budget_usd,
            "alert": "limit_reached" if spent + reserved >= self.settings.monthly_budget_usd
            else "approaching_limit" if spent + reserved >= self.settings.monthly_budget_usd * 0.8 else None,
        }

    def dispose(self):
        self.engine.dispose()
