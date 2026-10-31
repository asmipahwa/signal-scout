"""Typed settings for every Signal Scout process, read from environment variables (and `.env` in dev).

One `Settings` object replaces ad-hoc `os.environ` lookups so a missing or malformed variable fails
at startup instead of halfway through a job. Secrets are `SecretStr`, so they never appear in a
`repr`, a traceback, or a structured log line by accident.

The LLM_* variables are the same ones `signal_scout.enrichment.factory` reads; they are declared here
so they are validated and documented in one place.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

USER_AGENT = "SignalScout/0.1 (+https://github.com/asmipahwa/signal-scout)"


class Settings(BaseSettings):
    """All runtime configuration. Defaults match `docker-compose.yml`; secrets have no defaults."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)

    # --- PostgreSQL -------------------------------------------------------------------------------
    # Either set DATABASE_URL (e.g. a managed database or CI), or the POSTGRES_* parts used by compose.
    database_url: SecretStr | None = None
    postgres_user: str = "signal_scout"
    postgres_password: SecretStr = SecretStr("")
    postgres_host: str = "localhost"
    postgres_port: int = 5433  # compose publishes on 5433 so a local Postgres on 5432 is not hit
    postgres_db: str = "signal_scout"

    # --- S3 / MinIO -------------------------------------------------------------------------------
    s3_endpoint_url: str | None = "http://localhost:9000"  # None means real AWS S3
    s3_bucket: str = "signal-scout"
    s3_region: str = "us-east-1"
    s3_access_key_id: SecretStr = SecretStr("")
    s3_secret_access_key: SecretStr = SecretStr("")

    # --- Elasticsearch (used from Sprint 3) -------------------------------------------------------
    elasticsearch_url: str = "http://localhost:9200"
    elastic_password: SecretStr = SecretStr("")

    # --- LLM (read by signal_scout.enrichment.factory) --------------------------------------------
    llm_provider: str = "gemini"
    llm_model: str = ""
    llm_rpm: int | None = None
    llm_rpd: int | None = None
    llm_state_dir: str = ".state"
    llm_prompt_version: str = ""
    gemini_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    slack_webhook_url: SecretStr = SecretStr("")

    # CostGuard caps on paid LLM spend (USD).
    cost_daily_cap_usd: float = 0.50
    cost_total_cap_usd: float = 4.50

    # --- Ingestion ---------------------------------------------------------------------------------
    http_user_agent: str = USER_AGENT
    http_timeout_s: float = 20.0
    http_max_retries: int = 3
    # Per-domain token bucket: sustained requests/second and burst size.
    http_rate_per_domain: float = 0.5
    http_burst_per_domain: int = 2
    # Circuit breaker: disable a source after this many consecutive failed polls.
    circuit_breaker_threshold: int = Field(default=5, ge=1)
    excerpt_chars: int = 500

    # --- Worker / scheduler ------------------------------------------------------------------------
    worker_poll_s: float = 2.0
    scheduler_tick_s: int = 60
    job_lock_timeout_s: int = 900

    log_level: str = "INFO"

    @property
    def sqlalchemy_url(self) -> str:
        """The async SQLAlchemy URL. Contains the password: never log it."""
        if self.database_url is not None:
            return self.database_url.get_secret_value()
        password = quote(self.postgres_password.get_secret_value(), safe="")
        return (
            f"postgresql+asyncpg://{quote(self.postgres_user, safe='')}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, built once."""
    return Settings()
