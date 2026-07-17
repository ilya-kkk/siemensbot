from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.bots import admin_bot


@pytest.mark.asyncio
async def test_existing_business_status_is_edited_and_re_pinned(monkeypatch) -> None:
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        send_message=AsyncMock(),
    )
    pin = AsyncMock()
    monkeypatch.setattr(admin_bot, "_read_cached_status_ids", Mock(return_value=(202, 303)))
    monkeypatch.setattr(
        admin_bot, "_render_business_status_message", AsyncMock(return_value="summary")
    )
    monkeypatch.setattr(admin_bot, "_pin_status_message", pin)

    await admin_bot._upsert_pinned_admin_status(bot, "business", ensure_pinned=True)

    bot.edit_message_text.assert_awaited_once_with(
        "summary", chat_id=202, message_id=303, parse_mode="HTML"
    )
    bot.send_message.assert_not_awaited()
    pin.assert_awaited_once_with(bot, 202, 303)


@pytest.mark.asyncio
async def test_missing_status_message_is_recreated_and_cached(monkeypatch) -> None:
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(side_effect=RuntimeError("message missing")),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=404)),
    )
    cache = Mock()
    pin = AsyncMock()
    monkeypatch.setattr(admin_bot, "_read_cached_status_ids", Mock(return_value=(202, 303)))
    monkeypatch.setattr(
        admin_bot, "_render_business_status_message", AsyncMock(return_value="summary")
    )
    monkeypatch.setattr(admin_bot, "_cache_status_message_id", cache)
    monkeypatch.setattr(admin_bot, "_pin_status_message", pin)

    await admin_bot._upsert_pinned_admin_status(bot, "business")

    bot.send_message.assert_awaited_once_with(202, "summary", parse_mode="HTML")
    cache.assert_called_once_with("business", 404)
    pin.assert_awaited_once_with(bot, 202, 404)


@pytest.mark.asyncio
async def test_business_database_failure_does_not_edit_last_snapshot(monkeypatch) -> None:
    bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
    monkeypatch.setattr(admin_bot, "_read_cached_status_ids", Mock(return_value=(202, 303)))
    monkeypatch.setattr(
        admin_bot,
        "_render_business_status_message",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await admin_bot._upsert_pinned_admin_status(bot, "business")

    bot.edit_message_text.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_business_chat_id_skips_update(monkeypatch) -> None:
    bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
    render = AsyncMock(return_value="summary")
    monkeypatch.setattr(admin_bot, "_read_cached_status_ids", Mock(return_value=(None, None)))
    monkeypatch.setattr(admin_bot, "_render_business_status_message", render)

    await admin_bot._upsert_pinned_admin_status(bot, "business")

    render.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_refreshes_pin_before_sending_menu(monkeypatch) -> None:
    events: list[str] = []
    message = SimpleNamespace(
        chat=SimpleNamespace(id=202),
        answer=AsyncMock(side_effect=lambda *_args, **_kwargs: events.append("menu")),
    )
    bot = SimpleNamespace()
    monkeypatch.setattr(
        admin_bot, "_ensure_admin", AsyncMock(return_value=(1, "business"))
    )
    monkeypatch.setattr(
        admin_bot,
        "_upsert_pinned_admin_status",
        AsyncMock(side_effect=lambda *_args, **_kwargs: events.append("status")),
    )

    await admin_bot.start(message, bot)

    assert events == ["status", "menu"]
    admin_bot._upsert_pinned_admin_status.assert_awaited_once_with(
        bot, "business", chat_id=202, ensure_pinned=True
    )


@pytest.mark.asyncio
async def test_authorized_start_still_sends_menu_when_database_is_unavailable(monkeypatch) -> None:
    message = SimpleNamespace(
        chat=SimpleNamespace(id=202),
        from_user=SimpleNamespace(username="business_admin"),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace()
    settings = SimpleNamespace(
        admin_role_for_username=Mock(return_value="business"),
    )
    monkeypatch.setattr(
        admin_bot, "_ensure_admin", AsyncMock(side_effect=RuntimeError("database unavailable"))
    )
    monkeypatch.setattr(admin_bot, "settings", settings)
    monkeypatch.setattr(admin_bot, "_cache_admin_chat_id", Mock())
    refresh = AsyncMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(admin_bot, "_upsert_pinned_admin_status", refresh)

    await admin_bot.start(message, bot)

    refresh.assert_awaited_once_with(bot, "business", chat_id=202, ensure_pinned=True)
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_tech_status_starts_with_summary(monkeypatch) -> None:
    repository = SimpleNamespace(
        get_admin_summary=AsyncMock(
            return_value={"start_24h": 12, "lead_24h": 3, "start_all": 148, "lead_all": 27}
        ),
        get_service_health=AsyncMock(return_value={}),
    )

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(admin_bot, "SessionLocal", SessionContext)
    monkeypatch.setattr(admin_bot, "AppRepository", lambda _session: repository)

    text = await admin_bot._render_tech_status_message()

    assert text.startswith("/start: 12 | lead: 3 | 24h\n/start: 148 | lead: 27 | all\n")
    assert text.index("/start: 12") < text.index("Siemensbot status")


@pytest.mark.asyncio
async def test_tech_status_keeps_diagnostics_when_database_is_unavailable(monkeypatch) -> None:
    class FailingSessionContext:
        async def __aenter__(self):
            raise RuntimeError("database unavailable")

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(admin_bot, "SessionLocal", FailingSessionContext)

    text = await admin_bot._render_tech_status_message()

    assert text.startswith("/start: — | lead: — | 24h\n/start: — | lead: — | all\n")
    assert "Database/heartbeats: 🚨 unavailable" in text
    assert "VPS/admin bot: ✅ alive" in text
