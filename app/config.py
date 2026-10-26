"""Application settings loaded from the environment."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven configuration.

    Values are read from (in order) real environment variables, ``.env.local``,
    ``.env`` in the current directory, and ``../.env`` (the repo root when the
    app runs from ``backend/``).
    """

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "relayboard-backend"
    app_env: str = "development"
    log_level: str = "INFO"
    log_json: bool = True

    # --- Database ---------------------------------------------------------
    # Direct connection or session pooler URL. The transaction pooler on port
    # 6543 is detected automatically and gets statement_cache_size=0.
    database_url: str
    database_pool_size: int = 5
    database_max_overflow: int = 5

    # --- Supabase auth ----------------------------------------------------
    supabase_url: str
    supabase_jwt_secret: str | None = None
    supabase_jwks_url: str | None = None
    supabase_jwt_audience: str = "authenticated"
    supabase_jwt_issuer: str | None = None
    jwks_cache_ttl: int = 3600

    # --- MQTT -------------------------------------------------------------
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_client_id: str | None = None
    mqtt_keepalive: int = 60
    mqtt_topic_prefix: str = "boards"
    mqtt_qos: int = 1
    mqtt_publish_timeout: float = 5.0
    mqtt_reconnect_min_delay: float = 1.0
    mqtt_reconnect_max_delay: float = 30.0

    # --- Reading batching -------------------------------------------------
    reading_batch_max_size: int = 100
    reading_batch_flush_interval: float = 0.2
    reading_queue_max_size: int = 10_000

    # --- HTTP -------------------------------------------------------------
    # NoDecode keeps pydantic-settings from demanding JSON, so the common
    # comma-separated form works. A JSON array is still accepted.
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                try:
                    return json.loads(text)
                except ValueError as exc:
                    raise ValueError(
                        "CORS_ORIGINS must be a JSON array or a comma-separated list"
                    ) from exc
            return [origin.strip() for origin in text.split(",") if origin.strip()]
        return value

    @field_validator(
        "supabase_jwt_secret",
        "supabase_jwks_url",
        "supabase_jwt_issuer",
        "mqtt_username",
        "mqtt_password",
        "mqtt_client_id",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def resolved_jwks_url(self) -> str:
        if self.supabase_jwks_url:
            return self.supabase_jwks_url
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

    @property
    def resolved_issuer(self) -> str:
        if self.supabase_jwt_issuer:
            return self.supabase_jwt_issuer
        return f"{self.supabase_url.rstrip('/')}/auth/v1"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()  # type: ignore[call-arg]
