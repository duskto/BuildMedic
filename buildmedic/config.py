from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
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
    model_provider: Literal["compatible", "deepseek"] = "compatible"
    model_thinking: Literal["enabled", "disabled"] = "disabled"
    github_token: str = ""
    github_log_cache_enabled: bool = True
    scorer_url: str = ""
    scorer_token: str = ""
    scorer_suite_sha256: str = ""
    diagnosis_review_key: str = ""
    diagnosis_rubric_sha256: str = ""
    runner_profiles: Path | None = None
    adaptive_enabled: bool = False
    cookie_secure: bool = False
    max_active_tasks: int = 2
    lease_seconds: int = 90
    max_model_calls: int = 32
    max_output_tokens: int = 4096
    model_timeout_seconds: int = 90
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    web_dist: Path = ROOT / "apps/web/dist"

    @field_validator("runner_profiles", mode="before")
    @classmethod
    def optional_profiles(cls, value):
        return None if value == "" else value

    @property
    def model_ready(self):
        return bool(self.model_base_url and self.model_api_key and self.model_id)

    @property
    def agent_tools_ready(self):
        return self.model_ready and (self.model_provider != "deepseek" or self.model_thinking == "disabled")

    @property
    def scoring_ready(self):
        import re

        return bool(
            self.scorer_url
            and len(self.scorer_token) >= 32
            and re.fullmatch(r"[a-f0-9]{64}", self.scorer_suite_sha256)
        )


@lru_cache
def settings():
    return Settings()
