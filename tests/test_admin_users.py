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


def test_admin_menu_and_handlers_expose_users() -> None:
    assert admin_bot.MENU.keyboard[0][2].text == "Юзеры"
    registered_handlers = [
        handler
        for handler in admin_bot.router.message.handlers
        if handler.callback is admin_bot.users
    ]
    assert len(registered_handlers) == 2
