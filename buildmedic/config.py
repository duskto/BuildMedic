from pathlib import Path
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
HARNESS_SHA = "1cfe254e8d25b15f37048c3730134686abe60335"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BUILDMEDIC_", env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./var/buildmedic.db"
    data_dir: Path = ROOT / "var"
    session_secret: str = ""
    runner_url: str = "http://127.0.0.1:8001"
    runner_token: str = ""
    runner_backend: str = "docker"
    runner_image: str = ""
    queue_backend: str = "database"
    redis_url: str = "redis://127.0.0.1:6379/0"
    model_base_url: str = ""
    model_api_key: str = ""
    model_id: str = ""
    github_token: str = ""
    harness2_path: str = ""
    cookie_secure: bool = False
    max_active_tasks: int = 2
    lease_seconds: int = 90
    max_model_calls: int = 12
    max_output_tokens: int = 4096

    @property
    def model_ready(self):
        return bool(self.model_base_url and self.model_api_key and self.model_id)


@lru_cache
def settings():
    return Settings()
