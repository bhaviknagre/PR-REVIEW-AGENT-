"""Environment-driven configuration; fails fast with a clear message on bad/missing values."""

from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerConfig(BaseSettings):
    """Worker/recovery knobs. No required fields, so the worker module can read it at import."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    worker_max_jobs: int = Field(default=4, ge=1)  # concurrent jobs per worker process
    worker_lease_seconds: int = Field(default=300, ge=1)  # lease TTL; NFR-8 recovery window
    job_deadline_seconds: int = Field(default=3600, ge=1)  # from job created_at, then dead-letter
    stage_timeout_seconds: float = Field(default=300.0, gt=0)  # for stages without a table entry
    analyse_timeout_seconds: float = Field(default=1200.0, gt=0)  # LLM stage (slow local models)
    sweep_interval_seconds: int = Field(default=30, ge=1, le=60)  # sweeper/recovery cron period
    workspace_root: str | None = None  # parent dir of ephemeral checkouts; None = system temp
    workspace_max_bytes: int = Field(default=1 << 30, ge=1)  # checkout cap (SPEC: repos to 1 GB)
    sandbox_max_repo_bytes: int = Field(default=1 << 30, ge=1)  # repo copy cap per sandbox run
    github_max_diff_bytes: int = Field(default=2_000_000, ge=1)  # PR diff size cap
    github_max_files: int = Field(default=300, ge=1)  # PR changed-file cap
    sandbox_memory_bytes: int = Field(default=512 << 20, ge=64 << 20)  # per sandbox container
    sandbox_cpus: float = Field(default=1.0, gt=0)  # per sandbox container
    test_timeout_seconds: float = Field(default=300.0, gt=0)  # test budget; repo config may lower
    retention_hour_utc: int = Field(default=3, ge=0, le=23)  # daily retention purge
    # Optional pip index override (e.g. a caching mirror); its host:port is the only extra
    # egress destination. Unset = real PyPI.
    sandbox_pip_index_url: str | None = None

    @field_validator("sandbox_pip_index_url")
    @classmethod
    def _check_index_url(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        u = urlsplit(v)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
            raise ValueError("must be http(s)://host[:port]/path without credentials")
        return v


class Settings(WorkerConfig):
    github_app_id: int
    github_private_key: SecretStr  # inline PEM, or loaded from github_private_key_path
    github_private_key_path: Path | None = None
    github_webhook_secret: SecretStr

    database_url: SecretStr
    redis_url: SecretStr

    llm_review_model: str = "ollama/qwen2.5-coder:7b"
    llm_triage_model: str = "ollama/qwen2.5-coder:1.5b"
    llm_fallback_models: list[str] = []
    llm_api_key: SecretStr | None = None  # not needed for local Ollama
    ollama_api_base: str = "http://localhost:11434"
    llm_job_budget_usd: float = Field(default=1.0, ge=0)
    llm_daily_budget_usd: float = Field(default=20.0, ge=0)

    admin_token: SecretStr | None = None  # unset disables the admin API (fail closed)

    retention_days: int = Field(default=30, ge=1)
    request_changes_on_critical: bool = False  # review event is COMMENT unless enabled

    @model_validator(mode="before")
    @classmethod
    def _load_private_key(cls, data: Any) -> Any:
        """Exactly one of GITHUB_PRIVATE_KEY / GITHUB_PRIVATE_KEY_PATH; the path is read now."""
        if not isinstance(data, dict):
            return data
        key, path = data.get("github_private_key"), data.get("github_private_key_path")
        if key and path:
            raise ValueError("set exactly one of GITHUB_PRIVATE_KEY or GITHUB_PRIVATE_KEY_PATH")
        if path:  # neither set: the required-field error names GITHUB_PRIVATE_KEY
            try:
                data = {**data, "github_private_key": Path(path).expanduser().read_text()}
            except OSError as exc:
                raise ValueError(f"cannot read GITHUB_PRIVATE_KEY_PATH: {exc.strerror}") from None
        return data


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # required values come from the environment
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])).upper()}: {e['msg']}" for e in exc.errors()
        )
        raise SystemExit(f"Invalid configuration: {problems}") from None
