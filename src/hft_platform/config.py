from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _default_sqlite_database_url() -> str:
    """Always under the repo root so cwd (uvicorn vs npm) does not fork two different DB files."""
    p = (_REPO_ROOT / "data" / "hft_platform.db").resolve()
    return f"sqlite+aiosqlite:///{p.as_posix()}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(default_factory=_default_sqlite_database_url)
    jwt_secret: str = "change-me-in-production-use-long-random-string"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24
    master_encryption_key: str = ""  # Fernet key base64; generated if empty at runtime (dev only)
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # When true, POST /bots/sessions/{id}/start spawns run_lite_worker.py on this machine (set false in prod).
    spawn_local_lite_worker: bool = True


def get_settings() -> Settings:
    return Settings()
