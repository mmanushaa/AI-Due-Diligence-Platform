from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration read from the environment (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/due_diligence"
    redis_url: str = "redis://localhost:6379/0"

    # Celery (step 1.9). Both default to REDIS_URL when unset.
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    # A task still unacked after this many seconds is delivered again. Keep it well
    # above the longest scanner run so a slow audit is not executed twice.
    celery_visibility_timeout: int = 3600

    # Intake limits (step 1.2). Every limit is enforced while streaming the
    # upload, never after the whole archive is already on disk.
    max_upload_bytes: int = 52_428_800
    max_uncompressed_bytes: int = 524_288_000
    max_file_count: int = 20_000
    max_compression_ratio: int = 100
    # Files above this size are recorded as excluded (step 1.3).
    max_file_bytes: int = 1_048_576
    upload_dir: Path = Path("./data/uploads")

    # Embeddings (step 2.3). `embedding_provider` is "hashing" (offline,
    # deterministic, no key needed) or "gateway" (the LLM gateway's /embeddings).
    embedding_provider: str = "hashing"
    embedding_model: str = "text-embedding-3-small"
    # Must equal models.EMBEDDING_DIM: the column is vector(1536) and changing it
    # needs a migration, so a mismatch is refused instead of silently truncated.
    embedding_dimensions: int = 1_536
    embedding_batch_size: int = 64
    embedding_timeout_seconds: float = 30.0

    # LLM gateway (LiteLLM). The key is a SecretStr so it cannot be logged by
    # accident; nothing prints it.
    litellm_base_url: str | None = None
    litellm_api_key: SecretStr | None = None

    # Model routing (step 2.5). These are the gateway's aliases, not provider
    # model ids: docker/litellm/config.yaml maps each alias to a real model, so
    # changing provider is a gateway change and not a code change. Triage is the
    # cheap model for first-pass classification; fallback is what a failed call
    # is retried against before the call is reported as failed.
    llm_default_model: str = "gpt-4o-mini"
    llm_triage_model: str = "gpt-4o-mini"
    llm_fallback_model: str = "gpt-4o"
    llm_fallback_enabled: bool = True
    llm_max_output_tokens: int = 1_024
    llm_temperature: float = 0.0
    llm_timeout_seconds: float = 60.0

    # Retry policy: attempts per model, then exponential backoff between them.
    llm_max_attempts: int = 3
    llm_retry_base_seconds: float = 0.5
    llm_retry_max_seconds: float = 8.0

    # USD per 1M tokens for a model the price table in app.llm.client does not
    # know. Override it when routing to a provider with different prices rather
    # than editing the table.
    llm_price_input_usd_per_1m: float = 0.15
    llm_price_output_usd_per_1m: float = 0.60


settings = Settings()
