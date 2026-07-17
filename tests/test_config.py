import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**values: str) -> Settings:
    defaults = {
        "ADMIN_BOT_TOKEN": "admin-token",
        "TECH_ADMIN_USERNAME": "@ilya_kkk",
    }
    defaults.update(values)
    return Settings(_env_file=None, DATABASE_URL="postgresql://localhost/example", **defaults)


def test_public_base_url_is_optional_in_production_and_local() -> None:
    production = _settings(APP_ENV="Production", PUBLIC_BASE_URL="https://bot.example")
    production_without_url = _settings(APP_ENV="Production", PUBLIC_BASE_URL="")
    local = _settings(APP_ENV="local", PUBLIC_BASE_URL="")

    assert production.public_base_url == "https://bot.example"
    assert not production_without_url.public_base_url
    assert not local.public_base_url


def test_stt_model_has_default_and_environment_override() -> None:
    default = _settings()
    overridden = _settings(OPENROUTER_STT_MODEL="vendor/russian-stt")

    assert default.openrouter_stt_model == "openai/gpt-4o-mini-transcribe"
    assert overridden.openrouter_stt_model == "vendor/russian-stt"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"ADMIN_BOT_TOKEN": ""}, "ADMIN_BOT_TOKEN"),
        ({"TECH_ADMIN_USERNAME": ""}, "TECH_ADMIN_USERNAME"),
    ],
)
def test_production_requires_technical_alert_recipient(
    values: dict[str, str], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _settings(APP_ENV="production", PUBLIC_BASE_URL="https://bot.example", **values)


@pytest.mark.parametrize(
    "value",
    [
        "bot.example",
        "ftp://bot.example",
        "https://bad host",
        "https://bot.example:bad",
        "https://bot.example?query=1",
        "https://bot.example#fragment",
    ],
)
def test_public_base_url_must_be_absolute_http_url(value: str) -> None:
    with pytest.raises(ValidationError, match="absolute HTTP"):
        _settings(APP_ENV="Production", PUBLIC_BASE_URL=value)
