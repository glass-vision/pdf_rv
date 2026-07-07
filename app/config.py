from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized configuration.

    The same settings are used by local `uv run ...` and Docker.
    Configure storage through `.env`:
      PDF_STORAGE_MODE=binary|file
    """

    app_env: str = "development"
    app_data_dir: Path = Path("./data")
    app_port: int = 8000
    upload_worker_count: int = 2
    pdf_storage_mode: Literal["binary", "file"] = "binary"
    sqlite_locking_mode: Literal["auto", "normal", "exclusive"] = "auto"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def resolved_app_data_dir(self) -> Path:
        return self.app_data_dir.expanduser().resolve()

    @property
    def database_path(self) -> Path:
        return self.resolved_app_data_dir / "app.db"

    @property
    def pdf_storage_dir(self) -> Path:
        return self.resolved_app_data_dir / "pdf_storage"

    @property
    def effective_sqlite_locking_mode(self) -> Literal["normal", "exclusive"]:
        if self.sqlite_locking_mode != "auto":
            return self.sqlite_locking_mode
        return "normal"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def ensure_data_dirs() -> None:
    settings = get_settings()
    settings.resolved_app_data_dir.mkdir(parents=True, exist_ok=True)
    settings.pdf_storage_dir.mkdir(parents=True, exist_ok=True)
    (settings.resolved_app_data_dir / "uploads_tmp").mkdir(parents=True, exist_ok=True)
