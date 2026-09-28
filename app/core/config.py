# Reads all settings from .env — the single source of truth for configuration.
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret(name: str, env_fallback: str) -> str:
    """Read a Docker secret file first; fall back to the env-var value."""
    secret_path = Path(f"/run/secrets/{name}")
    if secret_path.exists():
        content = secret_path.read_text().strip()
        lines = [line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")]
        if lines:
            val = lines[-1]
            # Ignore placeholder strings so valid .env configuration is not clobbered
            if not val.startswith("PLACEHOLDER_") and not val.startswith("replace_"):
                return val
    return env_fallback


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_env: Literal["development", "production"] = "development"
    log_level: str = "INFO"

    # Database
    database_url: str = "postgresql+asyncpg://satquery:password@localhost:5432/satquery_db"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Security
    secret_key: str = "change-me-in-production"
    api_key: str | None = None
    auth_required: bool = False
    allowed_origins: list[str] | str = ["http://localhost:3000"]

    @field_validator("allowed_origins", mode="after")
    @classmethod
    def parse_origins(cls, v: str | list) -> list[str]:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",")]
        return v

    @field_validator("database_url", mode="after")
    @classmethod
    def resolve_db_host(cls, v: str) -> str:
        import socket
        if "@db:" in v:
            try:
                socket.gethostbyname("db")
            except socket.gaierror:
                v = v.replace("@db:", "@localhost:")
        return v

    @field_validator("redis_url", mode="after")
    @classmethod
    def resolve_redis_host(cls, v: str) -> str:
        import socket
        if "redis://redis:" in v:
            try:
                socket.gethostbyname("redis")
            except socket.gaierror:
                v = v.replace("redis://redis:", "redis://localhost:")
        return v

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        """Refuse to start in production if CORS or secrets are misconfigured."""
        if self.app_env != "production":
            return self

        # ── CORS guard ────────────────────────────────────────────────────────
        bad_origins = []
        for origin in self.allowed_origins:
            if origin == "*":
                bad_origins.append(f"'{origin}' (wildcard not allowed in production)")
            elif "localhost" in origin or "127.0.0.1" in origin:
                bad_origins.append(f"'{origin}' (localhost not allowed in production)")
            elif origin.startswith("http://"):
                bad_origins.append(f"'{origin}' (HTTP not allowed — use HTTPS in production)")
        if bad_origins:
            raise ValueError(
                "PRODUCTION SECURITY ERROR — unsafe ALLOWED_ORIGINS detected:\n"
                + "\n".join(f"  ✗ {o}" for o in bad_origins)
                + "\n\nSet ALLOWED_ORIGINS to your actual HTTPS domain, e.g.:\n"
                "  ALLOWED_ORIGINS=https://satquery.yourdomain.com"
            )

        # ── Secret key guard ──────────────────────────────────────────────────
        if self.secret_key in ("change-me-in-production", "replace_with_64_char_random_hex_string", ""):
            raise ValueError(
                "PRODUCTION SECURITY ERROR — SECRET_KEY is a placeholder.\n"
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(64))\"\n"
                "Then set it via Docker Secret or environment variable."
            )

        return self

    # Storage
    # Local filesystem for MVP or MinIO / S3 distributed object storage for production.
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_root: str = "./data"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_bucket_name: str = "satquery"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_region: str = "us-east-1"
    s3_public_endpoint_url: str | None = None

    # Upload limits
    max_upload_size_mb: int = 500
    allowed_image_formats: list[str] | str = ["tif", "tiff", "jp2", "img", "hdf5", "nc", "png", "jpg", "jpeg"]

    @field_validator("allowed_image_formats", mode="after")
    @classmethod
    def parse_formats(cls, v: str | list) -> list[str]:
        if isinstance(v, str):
            return [f.strip().lower() for f in v.split(",")]
        return v

    # ── MODEL INJECTION POINT ──────────────────────────────────────────────────
    # Set VLM_MODEL_NAME in .env once you decide which model to use.
    # The model loader in app/core/model_provider.py reads this value.
    # ──────────────────────────────────────────────────────────────────────────
    vlm_model_name: str = "PLACEHOLDER_SET_WHEN_MODEL_IS_CHOSEN"
    vlm_device: str = "cpu"

    # ── LLM / ORCHESTRATOR INJECTION POINT ────────────────────────────────────
    # GPT-4o is selected as the default (best production results for agentic tasks).
    # Provide OPENAI_API_KEY in .env when you have it.
    # ──────────────────────────────────────────────────────────────────────────
    llm_provider: Literal["openai", "gemini", "ollama"] = "openai"
    llm_model: str = "gpt-4o"
    openai_api_key: str = "PLACEHOLDER_API_KEY_TO_BE_PROVIDED"
    google_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"

    # ── GEMINI VLM (Specialist Agents) ────────────────────────────────────────
    # Used by GeminiVisionClient in app/ai/gemini_client.py for image analysis.
    # If not set, the client operates in offline/heuristic mode automatically.
    # ──────────────────────────────────────────────────────────────────────────
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"

    def model_post_init(self, __context) -> None:
        """Override sensitive fields with Docker secrets when available."""
        self.secret_key = _read_secret("secret_key", self.secret_key)
        secret_api_key = _read_secret("api_key", "")
        if secret_api_key:
            self.api_key = secret_api_key
        self.google_api_key = _read_secret("gemini_api_key", self.google_api_key)
        self.gemini_api_key = _read_secret("gemini_api_key", self.gemini_api_key)
        if not self.google_api_key and self.gemini_api_key:
            self.google_api_key = self.gemini_api_key
        if not self.gemini_api_key and self.google_api_key:
            self.gemini_api_key = self.google_api_key

        # S3 / MinIO credentials: check standard AWS env vars first, then Docker secrets
        aws_key = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("S3_ACCESS_KEY")
        if aws_key:
            self.s3_access_key = aws_key
        aws_secret = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("S3_SECRET_KEY")
        if aws_secret:
            self.s3_secret_key = aws_secret

        self.s3_access_key = _read_secret("minio_root_user", self.s3_access_key)
        self.s3_secret_key = _read_secret("minio_root_password", self.s3_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
