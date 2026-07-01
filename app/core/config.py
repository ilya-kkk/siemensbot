from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, computed_field
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
    test_drive_url: str = Field(default="https://example.com/test-drive", alias="TEST_DRIVE_URL")
    public_base_url: str | None = Field(default=None, alias="PUBLIC_BASE_URL")
    tech_admin_username: str | None = Field(default=None, alias="TECH_ADMIN_USERNAME")
    business_admin_username: str | None = Field(default=None, alias="BUSINESS_ADMIN_USERNAME")
    app_env: str = Field(default="local", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    worker_poll_seconds: float = Field(default=5.0, alias="WORKER_POLL_SECONDS")
    worker_claim_limit: int = Field(default=25, alias="WORKER_CLAIM_LIMIT")

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
