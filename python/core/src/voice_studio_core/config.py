from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VOICE_", env_file=".env", extra="ignore")

    role: Literal["controller", "worker"] = "controller"
    hosted_models: list[str] = Field(default_factory=lambda: ["nano"])
    public_origin: str = ""
    mode: Literal["local", "hosted"] = "local"
    data_dir: Path = Path("data")
    model_dir: Path = Path("models")
    database_url: str = ""
    bind_host: str = "127.0.0.1"
    port: int = 8765
    local_token: SecretStr = SecretStr("")
    owner_password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    allowed_origins: list[str] = Field(default_factory=list)
    static_dir: Path = Path("apps/studio/dist")
    worker_enabled: bool = True
    device: Literal["cpu", "mps", "cuda"] = "cpu"
    max_upload_bytes: int = 50 * 1024 * 1024
    max_reference_seconds: int = 600
    max_queue: int = 20
    cpu_threads: int = Field(default=4, ge=1, le=64)
    local_engine_idle_seconds: int = Field(default=300, ge=0, le=3600)
    job_timeout_seconds: int = 900
    lease_seconds: int = 60
    max_attempts: int = 2
    retention_days: int = 30
    controller_idle_seconds: int = 600
    monthly_budget_usd: float = 50
    job_reservation_usd: float = 0.50
    worker_cpu: int = 4
    worker_memory_gib: int = 8
    worker_disk_gib: int = 10
    worker_gpu_type: str = ""
    worker_hourly_usd: float = 0.3312
    controller_hourly_usd: float = 0.0666
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
        if self.mode == "hosted":
            required = {
                "database_url": self.database_url.startswith("postgresql"),
                "s3_bucket": bool(self.s3_bucket),
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
