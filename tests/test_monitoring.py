from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import alerts
from app.core.config import Settings
from app.monitoring import (
    cache_business_admin_chat_id,
    cache_business_status_message_id,
    cache_tech_admin_chat_id,
    read_cached_business_admin_chat_id,
    read_cached_business_status_message_id,
    read_cached_tech_admin_chat_id,
)


def _settings(tmp_path: Path, **values: str) -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql://localhost/example",
        ADMIN_BOT_TOKEN="admin-secret-token",
        TECH_ADMIN_USERNAME="@ilya_kkk",
        TECH_ADMIN_CHAT_CACHE_PATH=tmp_path / "tech_chat",
        **values,
    )


def test_tech_admin_cache_round_trip_and_invalid_value(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "tech_chat"
    cache_tech_admin_chat_id(path, 123456)

    assert read_cached_tech_admin_chat_id(path) == 123456
    assert path.stat().st_mode & 0o777 == 0o600

    path.write_text("not-an-id", encoding="utf-8")
    assert read_cached_tech_admin_chat_id(path) is None


def test_business_status_cache_round_trip(tmp_path: Path) -> None:
    chat_path = tmp_path / "business_chat"
    message_path = tmp_path / "business_message"

    cache_business_admin_chat_id(chat_path, 202)
    cache_business_status_message_id(message_path, 303)

    assert read_cached_business_admin_chat_id(chat_path) == 202
    assert read_cached_business_status_message_id(message_path) == 303


@pytest.mark.asyncio
async def test_alert_uses_cache_and_redacts_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    cache_tech_admin_chat_id(settings.tech_admin_chat_cache_path, 777)
    sent: dict[str, object] = {}

    class _Bot:
        def __init__(self, token: str, **_kwargs) -> None:
            sent["token"] = token
            self.session = SimpleNamespace(close=AsyncMock())

        async def send_message(self, chat_id: int, text: str) -> None:
            sent.update(chat_id=chat_id, text=text)

    monkeypatch.setattr(alerts, "Bot", _Bot)
    monkeypatch.setattr(alerts, "_persist_alert", AsyncMock())
    alerts._last_sent.clear()

    delivered = await alerts.send_alert(
        object(),  # type: ignore[arg-type] - deliberately broken database session
        settings,
        "critical",
        "database",
        f"cannot connect using {settings.database_url} and {settings.admin_bot_token}",
        force=True,
    )

    assert delivered is True
    assert sent["chat_id"] == 777
    assert settings.database_url not in str(sent["text"])
    assert settings.admin_bot_token not in str(sent["text"])


@pytest.mark.asyncio
async def test_dependency_transients_alert_after_three_and_recover_after_three(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts, "send_alert", send)
    alerts._dependency_failures.clear()
    alerts._dependency_open.clear()
    alerts._dependency_successes.clear()
    settings = _settings(tmp_path)

    assert not await alerts.record_dependency_failure(settings, "user_bot", "openrouter", "500")
    assert not await alerts.record_dependency_failure(settings, "user_bot", "openrouter", "500")
    assert await alerts.record_dependency_failure(settings, "user_bot", "openrouter", "500")
    assert send.await_count == 1

    assert not await alerts.record_dependency_success(settings, "user_bot", "openrouter")
    assert not await alerts.record_dependency_success(settings, "user_bot", "openrouter")
    assert await alerts.record_dependency_success(settings, "user_bot", "openrouter")
    assert send.await_count == 2
    assert send.await_args.args[2] == "recovered"


@pytest.mark.asyncio
async def test_permanent_dependency_error_alerts_immediately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts, "send_alert", send)
    alerts._dependency_failures.clear()
    alerts._dependency_open.clear()

    result = await alerts.record_dependency_failure(
        _settings(tmp_path),
        "user_bot",
        "openrouter",
        "unauthorized",
        status_code=401,
    )

    assert result is True
    send.assert_awaited_once()
