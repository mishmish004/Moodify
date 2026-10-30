"""
App settings from the environment, falling back to .env at the project root.

Nothing is read at import time: call get_settings(). A missing required setting
raises pydantic's ValidationError naming the field; we never sys.exit() here,
so tests and the app's startup can catch it.
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    ENV: Literal["dev", "test", "prod"] = "dev"
    SECRET_KEY: SecretStr = Field(..., description="signs the session cookie")

    # Spotify (Authorization Code + PKCE). PKCE doesn't need the client secret,
    # so it's optional until something actually uses it.
    SPOTIFY_CLIENT_ID: str = Field(..., min_length=1)
    SPOTIFY_CLIENT_SECRET: Optional[SecretStr] = None
    # Spotify rejects `localhost`; loopback must be 127.0.0.1, anything else HTTPS
    SPOTIFY_REDIRECT_URI: str = "http://127.0.0.1:9091/callback"

    # YouTube Music (Phase 27): Google sign-in for the YouTube Data API. Unset: only Spotify is offered
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[SecretStr] = None
    GOOGLE_REDIRECT_URI: str = "http://127.0.0.1:9091/callback/youtube"
    # The Google Cloud project's daily YouTube Data API units; raise it if Google grants more
    YOUTUBE_DAILY_QUOTA: int = Field(10_000, gt=0)

    # LLM that turns mood + taste into song suggestions
    OPENAI_API_KEY: SecretStr = Field(...)
    OPENAI_MODEL: str = "gpt-5.4-mini"
    # Reads the prompt into a brief (Phase 11); a smaller, faster model can do this. Unset: OPENAI_MODEL
    OPENAI_BRIEF_MODEL: Optional[str] = None
    # Judges how library tracks sound, 50 a call, in the background (Phase 22). Unset: OPENAI_MODEL
    OPENAI_FEATURES_MODEL: Optional[str] = None
    # Recalls lessons by meaning (Phase 31). The thresholds in src/embeddings.py were measured on this model;
    # another one needs them measured again. Empty: lessons are recalled by their tags alone
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-large"

    DB_PATH: Path = PROJECT_ROOT / "data" / "moodify.db"
    BASE_URL: str = "http://127.0.0.1:9091"
    # Where the privacy page says to send questions (YouTube API policy III.A.2.i). Unset: "ask whoever sent the link"
    CONTACT_EMAIL: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        # Otherwise a "field required" error prints every loaded value, API keys included
        hide_input_in_errors=True,
    )

    @model_validator(mode="after")
    def check_redirect_uri(self) -> "Settings":
        # Catch what Spotify would reject at login time, before anyone clicks Login
        if urlsplit(self.SPOTIFY_REDIRECT_URI).hostname == "localhost":
            raise ValueError("SPOTIFY_REDIRECT_URI: Spotify rejects localhost, use 127.0.0.1")
        if self.ENV == "prod" and not self.SPOTIFY_REDIRECT_URI.startswith("https://"):
            raise ValueError("SPOTIFY_REDIRECT_URI must use https:// in prod")
        if self.GOOGLE_CLIENT_ID and not self.GOOGLE_CLIENT_SECRET:
            raise ValueError("GOOGLE_CLIENT_SECRET is needed with GOOGLE_CLIENT_ID: Google's web clients send both")
        if self.GOOGLE_CLIENT_ID and self.ENV == "prod" and not self.GOOGLE_REDIRECT_URI.startswith("https://"):
            raise ValueError("GOOGLE_REDIRECT_URI must use https:// in prod")
        return self

    @property
    def youtube_enabled(self) -> bool:
        return bool(self.GOOGLE_CLIENT_ID)


@lru_cache()
def get_settings() -> Settings:
    return Settings()
