from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VOICE_", env_file=".env", extra="ignore")

    profile: Literal["local", "hosted_external", "hosted_daytona_single"] | None = None
    storage_backend: Literal["local", "s3", "daytona_volume"] | None = None
    volume_dir: Path = Path("/mnt/voice-objects")
    checkpoint_dir: Path = Path("/mnt/voice-checkpoints")
    control_token: SecretStr = SecretStr("")
    backup_key: SecretStr = SecretStr("")
    launcher_dir: Path = Path.home() / ".voice-studio" / "deployments"
    deployment_region: str | None = None
    single_hourly_usd: float = Field(default=0.3312, ge=0, allow_inf_nan=False)
    migrate_on_start: bool = True
    pool_size: int = Field(default=5, ge=1, le=10)
    role: Literal["controller", "worker"] = "controller"
    hosted_models: list[str] = Field(default_factory=lambda: ["nano"])
    public_origin: str = ""
    mode: Literal["local", "hosted"] = "local"
    data_dir: Path = Path("data")
    model_dir: Path = Path("models")
    database_url: str = ""
    bind_host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    local_token: SecretStr = SecretStr("")
    local_token_ttl_seconds: int = Field(default=8 * 3600, ge=60, le=24 * 3600)
    owner_password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    allowed_origins: list[str] = Field(default_factory=list)
    static_dir: Path = Path("apps/studio/dist")
    worker_enabled: bool = True
    device: Literal["cpu", "mps", "cuda"] = "cpu"
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, gt=0, le=50 * 1024 * 1024)
    max_reference_seconds: int = Field(default=600, gt=5, le=600)
    max_queue: int = Field(default=20, ge=1, le=1000)
    cpu_threads: int = Field(default=4, ge=1, le=64)
    local_engine_idle_seconds: int = Field(default=300, ge=0, le=3600)
    job_timeout_seconds: int = Field(default=900, gt=0)
    lease_seconds: int = Field(default=60, gt=0)
    max_attempts: int = Field(default=2, ge=1, le=10)
    retention_days: int = Field(default=30, ge=1, le=30)
    controller_idle_seconds: int = Field(default=600, gt=0)
    monthly_budget_usd: float = Field(default=50, gt=0, allow_inf_nan=False)
    job_reservation_usd: float = Field(default=0.50, gt=0, allow_inf_nan=False)
    worker_cpu: int = Field(default=4, ge=1)
    worker_memory_gib: int = Field(default=8, ge=1)
    worker_disk_gib: int = Field(default=10, ge=1)
    worker_gpu_type: str = ""
    worker_hourly_usd: float = Field(default=0.3312, ge=0, allow_inf_nan=False)
    controller_hourly_usd: float = Field(default=0.0666, ge=0, allow_inf_nan=False)
    s3_endpoint: str = ""
    s3_region: str = "us-east-1"
    s3_bucket: str = ""
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    daytona_api_key: SecretStr = SecretStr("")
    daytona_api_url: str = "https://app.daytona.io/api"
    daytona_controller_snapshot: str = ""
    daytona_worker_snapshot: str = ""
    daytona_secret_names: dict[str, str] = Field(default_factory=dict)
    deployment_id: str = "voice-studio"
    sandbox_id: str = ""

    @model_validator(mode="after")
    def validate_hosted(self):
        if self.profile is None:
            self.profile = "local" if self.mode == "local" else "hosted_external"
        else:
            self.mode = "local" if self.profile == "local" else "hosted"
        expected_storage = {"local": "local", "hosted_external": "s3", "hosted_daytona_single": "daytona_volume"}[self.profile]
        self.storage_backend = self.storage_backend or expected_storage
        if self.storage_backend != expected_storage:
            raise ValueError("Storage backend does not match the deployment profile")
        if self.profile == "hosted_daytona_single":
            import base64
            try:
                key = base64.b64decode(self.backup_key.get_secret_value(), validate=True)
            except ValueError:
                key = b""
            if len(key) != 32:
                raise ValueError("A protected 32-byte checkpoint key is required")
            if self.device != "cpu":
                raise ValueError("The single-sandbox profile supports CPU only")
        if self.mode == "hosted":
            required = {
                "database_url": self.database_url.startswith("postgresql"),
                "s3_bucket": self.storage_backend != "s3" or bool(self.s3_bucket),
                "owner_password_hash": self.role == "worker"
                or bool(self.owner_password_hash.get_secret_value()),
                "session_secret": self.role == "worker"
                or len(self.session_secret.get_secret_value()) >= 32,
            }
            missing = [name for name, valid in required.items() if not valid]
            if missing:
                raise ValueError(f"Hosted configuration is incomplete: {', '.join(missing)}")
        if self.mode == "local" and self.database_url and not self.database_url.startswith("sqlite"):
            raise ValueError("Local mode requires SQLite; hosted databases require an explicit hosted workspace")
        if self.bind_host not in {"127.0.0.1", "localhost", "::1"} and self.mode == "local":
            raise ValueError("Local mode must bind to loopback")
        if self.max_attempts < 1 or self.monthly_budget_usd <= 0:
            raise ValueError("Attempts and budget must be positive")
        return self

    def prepare(self):
        self.data_dir = self.data_dir.resolve()
        self.model_dir = self.model_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir / 'studio.sqlite3'}"
        return self
