from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bots import admin_bot


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.mark.asyncio
async def test_users_sends_all_rendered_rich_messages(monkeypatch) -> None:
    data = {"daily": []}
    repository = SimpleNamespace(get_users_by_start_day=AsyncMock(return_value=data))
    message = SimpleNamespace(answer_rich=AsyncMock())

    monkeypatch.setattr(admin_bot, "_ensure_admin", AsyncMock(return_value=(1, "business")))
    monkeypatch.setattr(admin_bot, "SessionLocal", _SessionContext)
    monkeypatch.setattr(admin_bot, "AppRepository", lambda _session: repository)
    monkeypatch.setattr(
        admin_bot,
        "render_users_rich_html",
        lambda value: ["<h1>Юзеры · часть 1/2</h1>", "<h1>Юзеры · часть 2/2</h1>"],
    )

    await admin_bot.users(message)

    repository.get_users_by_start_day.assert_awaited_once_with()
    assert message.answer_rich.await_count == 2
    assert [
        call.args[0].html for call in message.answer_rich.await_args_list
    ] == ["<h1>Юзеры · часть 1/2</h1>", "<h1>Юзеры · часть 2/2</h1>"]
    assert all(
        call.kwargs["reply_markup"] is admin_bot.MENU
        for call in message.answer_rich.await_args_list
    )
    assert all(
        call.args[0].skip_entity_detection is False
        for call in message.answer_rich.await_args_list
    )


@pytest.mark.asyncio
async def test_dialogue_shortcut_returns_dialogue_and_clears_state(monkeypatch) -> None:
    rows = [{"text": "Привет"}]
    repository = SimpleNamespace(get_user_messages_by_id=AsyncMock(return_value=rows))
    message = SimpleNamespace(text="/dialog_42", answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())

    monkeypatch.setattr(admin_bot, "_ensure_admin", AsyncMock(return_value=(1, "tech")))
    monkeypatch.setattr(admin_bot, "SessionLocal", _SessionContext)
    monkeypatch.setattr(admin_bot, "AppRepository", lambda _session: repository)
    monkeypatch.setattr(admin_bot, "render_dialogue_html", lambda value: "rendered-dialogue")
    monkeypatch.setattr(admin_bot, "split_telegram_html", lambda value: ["part-1", "part-2"])

    await admin_bot.dialogue_shortcut(message, state)

    state.clear.assert_awaited_once_with()
    repository.get_user_messages_by_id.assert_awaited_once_with(42)
    assert [call.args for call in message.answer.await_args_list] == [("part-1",), ("part-2",)]
    assert all(
        call.kwargs == {"parse_mode": "HTML"} for call in message.answer.await_args_list
    )


def test_admin_menu_and_handlers_expose_users() -> None:
    assert admin_bot.MENU.keyboard[0][2].text == "Юзеры"
    assert [button.text for button in admin_bot.MENU.keyboard[1]] == [
        "Диалог",
        "Линк",
        "Пинги",
    ]
    assert [button.text for button in admin_bot.MENU.keyboard[2]] == [
        "Алерт",
        "Стоп",
        "Отмена",
    ]
    registered_handlers = [
        handler
        for handler in admin_bot.router.message.handlers
        if handler.callback is admin_bot.users
    ]
    assert len(registered_handlers) == 2
    shortcut_handlers = [
        handler
        for handler in admin_bot.router.message.handlers
        if handler.callback is admin_bot.dialogue_shortcut
    ]
    assert len(shortcut_handlers) == 1
    link_handlers = [
        handler
        for handler in admin_bot.router.message.handlers
        if handler.callback is admin_bot.generate_referral_link_start
    ]
    assert len(link_handlers) == 2


@pytest.mark.asyncio
async def test_generate_referral_link_creates_source_and_returns_deep_link(monkeypatch) -> None:
    repository = SimpleNamespace(
        create_referral_source=AsyncMock(
            return_value={"id": 7, "source_code": "link7", "title": "Telegram Ads"}
        )
    )
    message = SimpleNamespace(
        text="Telegram Ads",
        from_user=SimpleNamespace(username="business_admin"),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())

    monkeypatch.setattr(admin_bot, "_ensure_admin", AsyncMock(return_value=(5, "business")))
    monkeypatch.setattr(admin_bot, "_get_user_bot_username", AsyncMock(return_value="siemens_user_bot"))
    monkeypatch.setattr(admin_bot, "SessionLocal", _SessionContext)
    monkeypatch.setattr(admin_bot, "AppRepository", lambda _session: repository)

    await admin_bot.generate_referral_link_receive(message, state)

    repository.create_referral_source.assert_awaited_once_with(
        "Telegram Ads",
        5,
        "business_admin",
    )
    state.clear.assert_awaited_once_with()
    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "Название: <b>Telegram Ads</b>" in text
    assert "ID: <code>link7</code>" in text
    assert "Ссылка: https://t.me/siemens_user_bot?start=link7" in text
    assert message.answer.await_args.kwargs["parse_mode"] == "HTML"
    assert message.answer.await_args.kwargs["reply_markup"] is admin_bot.MENU
