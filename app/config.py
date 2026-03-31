"""Application configuration using Pydantic Settings v2."""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import AnyHttpUrl, Field, RedisDsn, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # Application
    # -------------------------------------------------------------------------
    app_env: str = Field(default="development")
    app_debug: bool = Field(default=False)
    secret_key: str = Field(default="change-me-in-production-use-at-least-32-chars")
    allowed_hosts: list[str] = Field(default=["localhost", "127.0.0.1"])

    # -------------------------------------------------------------------------
    # Database
    # -------------------------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://asyncjobs:changeme@localhost:5432/asyncjobs"
    )
    database_pool_size: int = Field(default=10)
    database_max_overflow: int = Field(default=20)
    database_pool_timeout: int = Field(default=30)

    # -------------------------------------------------------------------------
    # Redis
    # -------------------------------------------------------------------------
    redis_url: str = Field(default="redis://localhost:6379/0")
    celery_broker_url: str = Field(default="redis://localhost:6379/0")
    celery_result_backend: str = Field(default="redis://localhost:6379/1")
    celery_task_always_eager: bool = Field(default=False)
    celery_task_result_expires: int = Field(default=86400)  # 24h

    # -------------------------------------------------------------------------
    # Auth
    # -------------------------------------------------------------------------
    jwt_secret_key: str = Field(
        default="change-me-jwt-secret-at-least-32-chars"
    )
    jwt_algorithm: str = Field(default="HS256")
    jwt_access_token_expire_minutes: int = Field(default=60)

    # -------------------------------------------------------------------------
    # Observability
    # -------------------------------------------------------------------------
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317"
    )
    otel_service_name: str = Field(default="async-jobs-api")
    log_level: str = Field(default="INFO")

    # -------------------------------------------------------------------------
    # Rate limiting
    # -------------------------------------------------------------------------
    rate_limit_per_minute: int = Field(default=60)

    # -------------------------------------------------------------------------
    # Celery DLQ
    # -------------------------------------------------------------------------
    dlq_queue_name: str = Field(default="dlq")
    default_queue_name: str = Field(default="default")

    # -------------------------------------------------------------------------
    # Task whitelist (security: prevent task injection)
    # -------------------------------------------------------------------------
    allowed_task_names: set[str] = Field(
        default={"workers.tasks.email_task.send_email", "workers.tasks.email_task.send_bulk_email"}
    )

    # -------------------------------------------------------------------------
    # Health check timeouts
    # -------------------------------------------------------------------------
    health_check_timeout_seconds: float = Field(default=2.0)

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        return v

    @field_validator("jwt_secret_key")
    @classmethod
    def validate_jwt_secret_key(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
