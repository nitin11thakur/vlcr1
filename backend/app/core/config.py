"""
app/core/config.py
------------------
Application configuration loaded from environment variables via pydantic-settings.
Missing API keys log a WARNING but do not crash the application (Requirement 1.6).
"""

import json
import logging
from typing import List, Optional, Union

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Application ────────────────────────────────────────────────────────────
    DEBUG: bool = False
    SECRET_KEY: str = "vlcr-change-this-in-production"

    # ── Database / Redis ───────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://vlcr:vlcr_pass@localhost:5432/vlcr"
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── CORS ───────────────────────────────────────────────────────────────────
    # FIX: Default is now restrictive (localhost only), not ["*"].
    # Wildcards are a security risk — configure explicit origins in production.
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:8080",
        "http://127.0.0.1:5500",
        "null",  # file:// origin for local HTML testing
    ]

    # ── AI — Claude ────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: Optional[str] = None
    # FIX: Corrected model string. "claude-opus-4-20250514" was invalid.
    # Use "claude-sonnet-4-6" as default (smart + efficient);
    # override with CLAUDE_MODEL=claude-opus-4-6 in .env for highest capability.
    CLAUDE_MODEL: str = "claude-sonnet-4-6"

    # ── Bhashini ───────────────────────────────────────────────────────────────
    BHASHINI_API_KEY: Optional[str] = None
    BHASHINI_USER_ID: Optional[str] = None

    # ── Exotel IVR ─────────────────────────────────────────────────────────────
    EXOTEL_API_KEY: Optional[str] = None
    EXOTEL_API_TOKEN: Optional[str] = None
    EXOTEL_SID: Optional[str] = None

    # ── SMS ────────────────────────────────────────────────────────────────────
    SMS_PROVIDER: str = "mock"
    GUPSHUP_API_KEY: Optional[str] = None
    GUPSHUP_APP_ID: Optional[str] = None
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    TWILIO_FROM_NUMBER: Optional[str] = None

    # ── AWS / S3 ───────────────────────────────────────────────────────────────
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    AWS_REGION: str = "ap-south-1"
    S3_BUCKET_AUDIO: str = "vlcr-audio-recordings"

    # ── Pipeline thresholds ────────────────────────────────────────────────────
    MIN_CLASSIFIER_CONFIDENCE: float = 0.70
    MIN_ASR_CONFIDENCE: float = 0.75

    # ── Auth ───────────────────────────────────────────────────────────────────
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480  # 8 hours

    # ── Compliance ─────────────────────────────────────────────────────────────
    AUDIT_RETENTION_YEARS: int = 7

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Union[str, list]) -> list:
        """Accept either a JSON array string or a comma-separated string."""
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @model_validator(mode="after")
    def check_secret_key_in_production(self) -> "Settings":
        """Refuse to start in production with the default SECRET_KEY."""
        if not self.DEBUG and self.SECRET_KEY == "vlcr-change-this-in-production":
            raise ValueError(
                "SECRET_KEY must be changed from the default value before running in production. "
                "Set DEBUG=true to suppress this check during local development."
            )
        return self

    def warn_missing_keys(self) -> None:
        """Log warnings for missing optional API keys (Requirement 1.6)."""
        checks = [
            (self.ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY", "AI classification (Claude)"),
            (self.BHASHINI_API_KEY, "BHASHINI_API_KEY", "ASR and translation (Bhashini)"),
            (self.BHASHINI_USER_ID, "BHASHINI_USER_ID", "ASR and translation (Bhashini)"),
            (self.EXOTEL_API_KEY, "EXOTEL_API_KEY", "IVR intake (Exotel)"),
            (self.EXOTEL_API_TOKEN, "EXOTEL_API_TOKEN", "IVR intake (Exotel)"),
            (self.EXOTEL_SID, "EXOTEL_SID", "IVR intake (Exotel)"),
        ]
        for value, key, feature in checks:
            if not value:
                logger.warning("Missing env var %s — feature degraded: %s", key, feature)

        if self.SMS_PROVIDER == "gupshup" and not self.GUPSHUP_API_KEY:
            logger.warning("Missing env var GUPSHUP_API_KEY — feature degraded: SMS notifications (Gupshup)")
        if self.SMS_PROVIDER == "twilio" and not self.TWILIO_ACCOUNT_SID:
            logger.warning("Missing env var TWILIO_ACCOUNT_SID — feature degraded: SMS notifications (Twilio)")


settings = Settings()
