import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SessionResponse(BaseModel):
    mode: Literal["local", "hosted"]
    profile: Literal["local", "hosted_external", "hosted_daytona_single"]
    local_access_expires_at: float | None = None


class JobState(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    LOADING = "loading"
    GENERATING = "generating"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATES = ["starting", "loading", "generating", "processing"]
TERMINAL_STATES = ["completed", "failed", "cancelled"]
AUDITION_TEXT = (
    "Hello, you've reached Voice Studio. Please leave your name and a message "
    "after the tone, and I will get back to you soon. Thank you for calling."
)


class SynthesisOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["original", "turbo", "nano"] = "nano"
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    temperature: float = Field(default=0.8, ge=0.1, le=1.2)
    exaggeration: float | None = Field(default=None, ge=0, le=1)
    cfg_weight: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def controls(self):
        if self.model != "original" and (
            self.exaggeration is not None or self.cfg_weight is not None
        ):
            raise ValueError("CFG and exaggeration are available only on Original")
        return self


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=2000)
    options: SynthesisOptions = Field(default_factory=SynthesisOptions)
    purpose: Literal["message", "audition"] = "message"

    @model_validator(mode="after")
    def clean_text(self):
        self.text = self.text.strip()
        if not self.text:
            raise ValueError("Enter a script")
        if len(self.text.split()) > 280:
            raise ValueError("Keep the script under 280 words (approximately two minutes)")
        if any(ord(char) < 32 and char not in "\n\t" for char in self.text):
            raise ValueError("Control characters are not supported")
        if "<" in self.text or ">" in self.text:
            raise ValueError("Use plain text; SSML and HTML are not supported")
        if self.purpose == "audition":
            self.text = AUDITION_TEXT
        return self

    def fingerprint(self):
        payload = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class ActivationRequest(BaseModel):
    audition_job_id: str
    approved: Literal[True]


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class KeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ErrorResponse(BaseModel):
    code: str
    message: str
    retryable: bool = False


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.status, self.retryable = code, message, status, retryable


class ReferenceDetails(BaseModel):
    model_config = ConfigDict(extra="allow")
    reference_duration: float
    sample_rate: int
    warnings: list[str]
    excerpt_start: float
    excerpt_end: float


class VoiceResponse(BaseModel):
    id: str
    name: str
    created_at: float
    active: bool
    authorized: bool
    checksum: str
    details: ReferenceDetails


class ArtifactResponse(BaseModel):
    id: str
    kind: str
    mime_type: str
    size: int
    checksum: str
    duration: float
    url: str


class JobResponse(BaseModel):
    id: str
    voice_id: str
    voice_name: str
    text: str
    state: JobState
    purpose: Literal["message", "audition"]
    options: SynthesisOptions
    created_at: float
    updated_at: float
    expires_at: float | None
    attempt: int
    cancel_requested: bool
    error: ErrorResponse | None
    timings: dict[str, float | None]
    artifacts: list[ArtifactResponse]
    created: bool | None = None


class InstallStatus(BaseModel):
    state: str
    file: str | None = None
    message: str | None = None
    completed_files: int | None = None
    total_files: int | None = None


class ModelCapability(BaseModel):
    id: Literal["original", "turbo", "nano"]
    revision: str
    download_bytes: int
    installed: bool
    configured_remote: bool = False
    supports_cfg: bool
    supports_exaggeration: bool
    install: InstallStatus | None = None


class Limits(BaseModel):
    script_characters: int
    script_words: int
    message_seconds: int
    queue: int


class CapabilitiesResponse(BaseModel):
    profile: Literal["local", "hosted_external", "hosted_daytona_single"] | None = None
    mode: Literal["local", "hosted"]
    device: str
    models: list[ModelCapability]
    limits: Limits
    performance_qualified: bool
    orchestration_error: str | None = None


class CheckpointStatus(BaseModel):
    revision: int
    created_at: float
    schema_version: int


class DurabilityStatus(BaseModel):
    enabled: bool
    state: Literal["saved", "storage_unavailable", "restoring"]
    checkpoint: CheckpointStatus | None = None


class OperationStatus(BaseModel):
    operation_id: str
    state: Literal["saved", "saving"]
    revision: int
