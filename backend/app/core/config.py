from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration read from the environment (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/due_diligence"
    redis_url: str = "redis://localhost:6379/0"

    # Intake limits (step 1.2). Every limit is enforced while streaming the
    # upload, never after the whole archive is already on disk.
    max_upload_bytes: int = 52_428_800
    max_uncompressed_bytes: int = 524_288_000
    max_file_count: int = 20_000
    max_compression_ratio: int = 100
    # Files above this size are recorded as excluded (step 1.3).
    max_file_bytes: int = 1_048_576
    upload_dir: Path = Path("./data/uploads")


settings = Settings()
