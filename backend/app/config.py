from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    database_url: str = "sqlite:///./geosyncai.db"
    jwt_secret: str = ""
    jwt_expire_minutes: int = Field(default=60, ge=5, le=480)
    jwt_issuer: str = "geosyncai"
    jwt_audience: str = "geosyncai-web"
    production_mode: bool = False
    api_docs_enabled: bool = False
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "[::1]", "testserver"]
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    trusted_proxy_cidrs: list[str] = []
    security_redis_url: str | None = None
    rate_limit_enabled: bool = True
    api_requests_per_minute: int = Field(default=600, ge=1)
    login_requests_per_minute: int = Field(default=20, ge=1)
    account_login_requests_per_window: int = Field(default=10, ge=1)
    account_login_window_seconds: int = Field(default=900, ge=1)
    heavy_requests_per_minute: int = Field(default=60, ge=1)
    max_concurrent_requests: int = Field(default=64, ge=1)
    max_concurrent_heavy_requests: int = Field(default=2, ge=1)
    max_json_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    request_read_timeout_seconds: int = Field(default=30, ge=1)
    request_body_timeout_seconds: int = Field(default=180, ge=1)
    max_active_jobs_per_project: int = Field(default=10, ge=1)
    storage_dir: str = "./storage"
    auto_bootstrap: bool = False
    demo_mode: bool = False
    celery_broker_url: str | None = None
    max_upload_bytes: int = 250 * 1024 * 1024
    max_zip_members: int = 10000
    max_zip_uncompressed_bytes: int = 1024 * 1024 * 1024
    max_zip_compression_ratio: int = Field(default=200, ge=1)
    max_source_records: int = Field(default=50000, ge=1)
    job_lease_seconds: int = 60
    job_heartbeat_seconds: int = 10
    job_max_attempts: int = 3
    field_max_assignments: int = 500
    field_max_photo_bytes: int = 5 * 1024 * 1024
    field_max_queued_bytes: int = 10 * 1024 * 1024
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def storage_path(self) -> Path:
        path = Path(self.storage_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
