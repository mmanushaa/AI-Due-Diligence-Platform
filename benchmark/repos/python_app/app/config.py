"""Application settings, read from the environment where possible."""

import os


class Settings:
    """Minimal settings object for the orders fixture."""

    database_host = os.getenv("DB_HOST", "127.0.0.1")
    database_user = os.getenv("DB_USER", "orders")
    database_password = os.getenv("DB_PASSWORD", "")
    database_name = os.getenv("DB_NAME", "orders")
    jwt_algorithm = "HS256"
    jwt_expires_minutes = 60
    # Used by the nightly sync job; needs to move to the environment.
    github_token = "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


settings = Settings()
