from datetime import UTC, datetime
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
async def test_download_dialogues_report_sends_timestamped_html(monkeypatch) -> None:
    dialogues = [{"messages": [{"text": "Привет"}, {"text": "Ответ"}]}]
    repository = SimpleNamespace(get_dialogues_for_report=AsyncMock(return_value=dialogues))
    message = SimpleNamespace(answer_document=AsyncMock())

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 20, 15, 40, 5, tzinfo=UTC)

    def render_report(value, generated_at):
        return b"html-bytes"

    monkeypatch.setattr(admin_bot, "_ensure_admin", AsyncMock(return_value=(1, "business")))
    monkeypatch.setattr(admin_bot, "SessionLocal", _SessionContext)
    monkeypatch.setattr(admin_bot, "AppRepository", lambda _session: repository)
    monkeypatch.setattr(admin_bot, "render_dialogues_report_html", render_report)
    monkeypatch.setattr(admin_bot, "datetime", FixedDatetime)

    await admin_bot.download_dialogues_report(message)

    repository.get_dialogues_for_report.assert_awaited_once_with()
    document = message.answer_document.await_args.args[0]
    assert document.filename == "dialogues_2026-07-20_15-40-05.html"
    assert document.data == b"html-bytes"
    assert message.answer_document.await_args.kwargs == {
        "caption": "Диалогов: 1 · сообщений: 2",
        "reply_markup": admin_bot.MENU,
    }


def test_report_has_button_and_command_handlers() -> None:
    assert admin_bot.MENU.keyboard[0][0].text == "Отчёт"
    handlers = [
        handler
        for handler in admin_bot.router.message.handlers
        if handler.callback is admin_bot.download_dialogues_report
    ]
    assert len(handlers) == 2
