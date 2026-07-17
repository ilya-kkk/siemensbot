from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _normalize_username(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lstrip("@").lower() or None


def _to_asyncpg_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        async_url = url
    elif url.startswith("postgres://"):
        async_url = "postgresql+asyncpg://" + url.removeprefix("postgres://")
    elif url.startswith("postgresql://"):
        async_url = "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    else:
        async_url = url

    parts = urlsplit(async_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("ssl", "require")
    query.setdefault("prepared_statement_cache_size", "0")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(alias="DATABASE_URL")
    admin_bot_token: str | None = Field(default=None, alias="ADMIN_BOT_TOKEN")
    user_bot_token: str | None = Field(default=None, alias="USER_BOT_TOKEN")
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(default="openai/gpt-4.1-mini", alias="OPENROUTER_MODEL")
    openrouter_stt_model: str = Field(
        default="openai/gpt-4o-mini-transcribe",
        alias="OPENROUTER_STT_MODEL",
    )
    welcome_text: str = Field(
        default="Привет! Давай разберём твой проект. В какой нише сейчас работаешь?",
        alias="WELCOME_TEXT",
    )
    public_base_url: str | None = Field(default=None, alias="PUBLIC_BASE_URL")
    tech_admin_username: str | None = Field(default=None, alias="TECH_ADMIN_USERNAME")
    business_admin_username: str | None = Field(default=None, alias="BUSINESS_ADMIN_USERNAME")
    app_env: str = Field(default="local", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    ping_worker_poll_seconds: float = Field(default=5.0, alias="PING_WORKER_POLL_SECONDS")
    ping_worker_lease_seconds: int = Field(default=600, alias="PING_WORKER_LEASE_SECONDS")
    ping_worker_retry_seconds: int = Field(default=300, alias="PING_WORKER_RETRY_SECONDS")
    heartbeat_interval_seconds: int = Field(default=30, alias="HEARTBEAT_INTERVAL_SECONDS")
    heartbeat_stale_seconds: int = Field(default=120, alias="HEARTBEAT_STALE_SECONDS")
    tech_admin_chat_cache_path: Path = Field(
        default=Path("runtime/tech_admin_chat_id"),
        alias="TECH_ADMIN_CHAT_CACHE_PATH",
    )
    tech_status_message_cache_path: Path = Field(
        default=Path("runtime/tech_status_message_id"),
        alias="TECH_STATUS_MESSAGE_CACHE_PATH",
    )
    business_admin_chat_cache_path: Path = Field(
        default=Path("runtime/business_admin_chat_id"),
        alias="BUSINESS_ADMIN_CHAT_CACHE_PATH",
    )
    business_status_message_cache_path: Path = Field(
        default=Path("runtime/business_status_message_id"),
        alias="BUSINESS_STATUS_MESSAGE_CACHE_PATH",
    )
    tech_status_update_seconds: int = Field(default=60, alias="TECH_STATUS_UPDATE_SECONDS")

    @model_validator(mode="after")
    def validate_public_base_url(self) -> "Settings":
        is_local = self.app_env.strip().lower() in {"local", "test"}
        public_base_url = (self.public_base_url or "").strip()
        if not is_local and not self.admin_bot_token:
            raise ValueError("ADMIN_BOT_TOKEN is required outside local/test environments")
        if not is_local and not self.tech_admin_username_normalized:
            raise ValueError("TECH_ADMIN_USERNAME is required outside local/test environments")
        if public_base_url:
            try:
                parsed = urlsplit(public_base_url)
                _ = parsed.port
            except ValueError as exc:
                raise ValueError("PUBLIC_BASE_URL must be an absolute HTTP(S) URL") from exc
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.netloc
                or not parsed.hostname
                or bool(parsed.query)
                or bool(parsed.fragment)
                or any(character.isspace() for character in public_base_url)
            ):
                raise ValueError("PUBLIC_BASE_URL must be an absolute HTTP(S) URL")
        return self

    @computed_field
    @property
    def async_database_url(self) -> str:
        return _to_asyncpg_url(self.database_url)

    @computed_field
    @property
    def tech_admin_username_normalized(self) -> str | None:
        return _normalize_username(self.tech_admin_username)

    @computed_field
    @property
    def business_admin_username_normalized(self) -> str | None:
        return _normalize_username(self.business_admin_username)

    def admin_role_for_username(self, username: str | None) -> Literal["tech", "business"] | None:
        normalized = _normalize_username(username)
        if normalized and normalized == self.tech_admin_username_normalized:
            return "tech"
        if normalized and normalized == self.business_admin_username_normalized:
            return "business"
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
