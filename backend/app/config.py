from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./geosyncai.db"
    jwt_secret: str = "change-this-in-production"
    jwt_expire_minutes: int = 480
    storage_dir: str = "./storage"
    auto_bootstrap: bool = True
    celery_broker_url: str | None = None
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def storage_path(self) -> Path:
        path = Path(self.storage_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
