from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./geosyncai.db"
    jwt_secret: str = ""
    jwt_expire_minutes: int = 480
    storage_dir: str = "./storage"
    auto_bootstrap: bool = False
    demo_mode: bool = False
    celery_broker_url: str | None = None
    max_upload_bytes: int = 250 * 1024 * 1024
    max_zip_members: int = 10000
    max_zip_uncompressed_bytes: int = 1024 * 1024 * 1024
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
